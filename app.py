import os
import io
import re
import json
import base64
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Depends, HTTPException, status, Header
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
from jose import JWTError, jwt
import bcrypt

import barcode
from barcode.writer import SVGWriter
import qrcode
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.graphics import renderPDF
from svglib.svglib import svg2rlg

# ==========================================
# 1. CONFIGURATION & PASSWORD UTILITIES
# ==========================================
SECRET_KEY = os.getenv("SECRET_KEY", "labelforge_ultra_secure_jwt_secret_key_2026_x995")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@nameofweb")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "Chopraji995#")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./labelforge.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def hash_password(password: str) -> str:
    pwd_bytes = password.encode('utf-8')[:72]
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(pwd_bytes, salt).decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        pwd_bytes = plain_password.encode('utf-8')[:72]
        hash_bytes = hashed_password.encode('utf-8')
        return bcrypt.checkpw(pwd_bytes, hash_bytes)
    except Exception:
        return False

# ==========================================
# 2. DATABASE MODELS
# ==========================================
class Business(Base):
    __tablename__ = "businesses"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    gstin = Column(String(30), default="")
    phone = Column(String(30), default="")
    address = Column(String(255), default="")
    city = Column(String(100), default="")
    state = Column(String(100), default="")
    country = Column(String(100), default="India")
    plan = Column(String(50), default="free")
    created_at = Column(DateTime, default=datetime.utcnow)

    users = relationship("User", back_populates="business")
    products = relationship("Product", back_populates="business")
    labels = relationship("SavedLabel", back_populates="business")

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=True)
    full_name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    phone = Column(String(30), default="")
    role = Column(String(20), default="member")
    is_suspended = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    business = relationship("Business", back_populates="users")

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    name = Column(String(255), nullable=False)
    sku = Column(String(100), nullable=False)
    barcode = Column(String(100), nullable=False)
    barcode_type = Column(String(50), default="ean13")
    category = Column(String(100), default="General")
    mrp = Column(Float, default=0.0)
    selling_price = Column(Float, default=0.0)
    hsn_code = Column(String(50), default="")
    gst_percent = Column(Float, default=0.0)
    batch_number = Column(String(50), default="")
    net_quantity = Column(String(50), default="1 unit")
    unit = Column(String(20), default="pcs")
    mfg_date = Column(String(50), default="")
    expiry_date = Column(String(50), default="")
    manufacturer = Column(String(255), default="")
    country_of_origin = Column(String(100), default="India")
    created_at = Column(DateTime, default=datetime.utcnow)

    business = relationship("Business", back_populates="products")

class SavedLabel(Base):
    __tablename__ = "saved_labels"
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    name = Column(String(255), nullable=False)
    width_mm = Column(Float, default=50.0)
    height_mm = Column(Float, default=25.0)
    canvas_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    business = relationship("Business", back_populates="labels")

class SystemSetting(Base):
    __tablename__ = "system_settings"
    id = Column(Integer, primary_key=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(Text, nullable=False)

Base.metadata.create_all(bind=engine)

def run_dev_migrations():
    with engine.connect() as conn:
        res = conn.exec_driver_sql("PRAGMA table_info(users)").fetchall()
        user_cols = [r[1] for r in res]
        if "phone" not in user_cols:
            conn.exec_driver_sql("ALTER TABLE users ADD COLUMN phone VARCHAR(30) DEFAULT ''")

        res_p = conn.exec_driver_sql("PRAGMA table_info(products)").fetchall()
        prod_cols = [r[1] for r in res_p]
        if "selling_price" not in prod_cols:
            conn.exec_driver_sql("ALTER TABLE products ADD COLUMN selling_price FLOAT DEFAULT 0.0")
        if "gst_percent" not in prod_cols:
            conn.exec_driver_sql("ALTER TABLE products ADD COLUMN gst_percent FLOAT DEFAULT 0.0")

try:
    run_dev_migrations()
except Exception:
    pass

# ==========================================
# 3. SEEDING & DATABASE DEPENDENCIES
# ==========================================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_defaults():
    db = SessionLocal()
    try:
        if not db.query(SystemSetting).filter(SystemSetting.key == "pricing_config").first():
            pricing_data = {
                "free_price": 0,
                "business_price": 799,
                "professional_price": 1999,
                "currency": "INR",
                "symbol": "₹"
            }
            db.add(SystemSetting(key="pricing_config", value=json.dumps(pricing_data)))
            db.commit()

        admin_user = db.query(User).filter(User.email == ADMIN_EMAIL).first()
        if not admin_user:
            biz = Business(name="Platform HQ", plan="professional")
            db.add(biz)
            db.commit()
            db.refresh(biz)
            admin_user = User(
                business_id=biz.id,
                full_name="Super Administrator",
                email=ADMIN_EMAIL,
                hashed_password=hash_password(ADMIN_PASSWORD),
                role="superadmin"
            )
            db.add(admin_user)
            db.commit()
    finally:
        db.close()

init_defaults()

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")

    user = db.query(User).filter(User.email == email).first()
    if not user or user.is_suspended:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account disabled or suspended")
    return user

# ==========================================
# 4. BARCODE VALIDATION & GENERATOR
# ==========================================
class BarcodeEngine:
    @staticmethod
    def calc_ean13_check(d12: str) -> int:
        odds = sum(int(x) for x in d12[0::2])
        evens = sum(int(x) for x in d12[1::2])
        total = odds + (evens * 3)
        mod = total % 10
        return 0 if mod == 0 else 10 - mod

    @classmethod
    def validate(cls, symbology: str, value: str) -> tuple[bool, str]:
        symbology = symbology.lower()
        clean = re.sub(r'[\s-]', '', value)
        if symbology == "ean13":
            nums = re.sub(r'\D', '', clean)
            if len(nums) == 12:
                return True, f"{nums}{cls.calc_ean13_check(nums)}"
            elif len(nums) == 13:
                expected = cls.calc_ean13_check(nums[:12])
                if int(nums[12]) != expected:
                    return False, f"Invalid check digit (Expected {expected})"
                return True, nums
            return False, "EAN-13 requires 12 or 13 numeric digits"
        elif symbology == "ean8":
            nums = re.sub(r'\D', '', clean)
            if len(nums) != 8:
                return False, "EAN-8 requires exactly 8 digits"
            return True, nums
        elif symbology == "upca":
            nums = re.sub(r'\D', '', clean)
            if len(nums) not in (11, 12):
                return False, "UPC-A requires 11 or 12 numeric digits"
            return True, nums
        elif symbology == "code128":
            if not value.isascii() or not value:
                return False, "Code 128 accepts standard alphanumeric ASCII characters"
            return True, value
        elif symbology == "code39":
            if not re.match(r'^[0-9A-Z\-\.\ \$\/\+\%]+$', value.upper()):
                return False, "Code 39 accepts uppercase letters, numbers, and - . $ / + %"
            return True, value.upper()
        elif symbology in ("qrcode", "qr"):
            if not value.strip():
                return False, "QR Code data cannot be empty"
            return True, value
        return False, f"Unsupported symbology '{symbology}'"

    @classmethod
    def generate_svg(cls, symbology: str, value: str, write_text: bool = True) -> str:
        valid, result = cls.validate(symbology, value)
        if not valid:
            raise ValueError(result)

        symbology = symbology.lower()
        if symbology in ("qrcode", "qr"):
            qr = qrcode.QRCode(box_size=10, border=1)
            qr.add_data(result)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode()
            return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200" width="100%" height="100%"><image href="data:image/png;base64,{b64}" width="200" height="200"/></svg>'

        b_class = barcode.get_barcode_class(symbology)
        writer = SVGWriter()
        bc = b_class(result, writer=writer)
        buf = io.BytesIO()
        bc.write(buf, options={"write_text": write_text, "quiet_zone": 2.0})
        return buf.getvalue().decode('utf-8')

# ==========================================
# 5. FASTAPI REST APIS
# ==========================================
app = FastAPI(title="LabelForge SaaS", version="2.5.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class RegisterSchema(BaseModel):
    full_name: str
    email: str
    phone: Optional[str] = ""
    password: str
    business_name: str
    business_type: Optional[str] = "Retail"
    plan: Optional[str] = "free"

class LoginSchema(BaseModel):
    email: str
    password: str

class ProductSchema(BaseModel):
    name: str
    sku: Optional[str] = None
    barcode: str
    barcode_type: str = "ean13"
    category: Optional[str] = "General"
    mrp: float = 0.0
    selling_price: float = 0.0
    hsn_code: Optional[str] = ""
    gst_percent: Optional[float] = 0.0
    batch_number: Optional[str] = ""
    net_quantity: Optional[str] = "1 unit"
    unit: Optional[str] = "pcs"
    mfg_date: Optional[str] = ""
    expiry_date: Optional[str] = ""
    manufacturer: Optional[str] = ""
    country_of_origin: Optional[str] = "India"

class LabelSaveSchema(BaseModel):
    name: str
    product_id: Optional[int] = None
    width_mm: float
    height_mm: float
    canvas_json: str

class SheetPdfSchema(BaseModel):
    items: List[Dict[str, Any]]
    label_width_mm: float = 50.0
    label_height_mm: float = 25.0

class PricingUpdateSchema(BaseModel):
    business_price: float
    professional_price: float

@app.get("/api/config/pricing")
def get_pricing(db: Session = Depends(get_db)):
    setting = db.query(SystemSetting).filter(SystemSetting.key == "pricing_config").first()
    if setting:
        return json.loads(setting.value)
    return {"free_price": 0, "business_price": 799, "professional_price": 1999, "symbol": "₹"}

@app.post("/api/admin/pricing")
def update_pricing(data: PricingUpdateSchema, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required")
    setting = db.query(SystemSetting).filter(SystemSetting.key == "pricing_config").first()
    if not setting:
        setting = SystemSetting(key="pricing_config", value="")
        db.add(setting)
    cfg = {"free_price": 0, "business_price": data.business_price, "professional_price": data.professional_price, "symbol": "₹"}
    setting.value = json.dumps(cfg)
    db.commit()
    return {"status": "updated", "pricing": cfg}

@app.post("/api/auth/register")
def register(data: RegisterSchema, db: Session = Depends(get_db)):
    clean_email = data.email.strip().lower()
    if db.query(User).filter(User.email == clean_email).first():
        raise HTTPException(status_code=400, detail="An account with this email already exists")

    biz = Business(
        name=data.business_name,
        phone=data.phone or "",
        plan=data.plan if data.plan in ("free", "business", "professional") else "free"
    )
    db.add(biz)
    db.commit()
    db.refresh(biz)

    user = User(
        business_id=biz.id,
        full_name=data.full_name,
        email=clean_email,
        phone=data.phone or "",
        hashed_password=hash_password(data.password),
        role="owner"
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token({"sub": user.email, "role": user.role, "biz": biz.id})
    return {
        "token": token,
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "business_name": biz.name,
            "plan": biz.plan
        }
    }

@app.post("/api/auth/login")
def login(data: LoginSchema, db: Session = Depends(get_db)):
    clean_email = data.email.strip().lower()
    user = db.query(User).filter(User.email == clean_email).first()
    if not user or not verify_password(data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if user.is_suspended:
        raise HTTPException(status_code=403, detail="Your account has been suspended.")

    biz_name = user.business.name if user.business else "Platform HQ"
    biz_plan = user.business.plan if user.business else "professional"
    token = create_access_token({"sub": user.email, "role": user.role, "biz": user.business_id})
    return {
        "token": token,
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "business_name": biz_name,
            "plan": biz_plan
        }
    }

@app.get("/api/products")
def get_products(query: Optional[str] = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not user.business_id:
        return []
    q = db.query(Product).filter(Product.business_id == user.business_id)
    if query and query.strip():
        term = f"%{query.strip().lower()}%"
        q = q.filter(
            (Product.name.ilike(term)) | (Product.sku.ilike(term)) | (Product.barcode.ilike(term))
        )
    return q.order_by(Product.created_at.desc()).all()

@app.post("/api/products")
def create_product(data: ProductSchema, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not user.business_id:
        raise HTTPException(status_code=400, detail="No business associated with user")

    valid, sanitized_or_err = BarcodeEngine.validate(data.barcode_type, data.barcode)
    if not valid:
        raise HTTPException(status_code=400, detail=f"Barcode Error: {sanitized_or_err}")

    sku_val = data.sku.strip() if data.sku and data.sku.strip() else None
    if not sku_val:
        count = db.query(Product).filter(Product.business_id == user.business_id).count()
        sku_val = f"PRD-{count + 1:06d}"

    existing = db.query(Product).filter(Product.business_id == user.business_id, Product.sku == sku_val).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"SKU '{sku_val}' is already in use.")

    prod_dict = data.model_dump()
    prod_dict["sku"] = sku_val
    prod_dict["barcode"] = sanitized_or_err

    prod = Product(business_id=user.business_id, **prod_dict)
    db.add(prod)
    db.commit()
    db.refresh(prod)
    return prod

@app.delete("/api/products/{prod_id}")
def delete_product(prod_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    p = db.query(Product).filter(Product.id == prod_id, Product.business_id == user.business_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    db.delete(p)
    db.commit()
    return {"status": "deleted"}

@app.post("/api/barcodes/render")
def render_barcode(symbology: str, value: str, text: bool = True):
    try:
        svg_str = BarcodeEngine.generate_svg(symbology, value, text)
        return Response(content=svg_str, media_type="image/svg+xml")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/labels")
def get_labels(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(SavedLabel).filter(SavedLabel.business_id == user.business_id).all()

@app.post("/api/labels")
def save_label(data: LabelSaveSchema, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    lbl = SavedLabel(
        business_id=user.business_id,
        product_id=data.product_id,
        name=data.name,
        width_mm=data.width_mm,
        height_mm=data.height_mm,
        canvas_json=data.canvas_json
    )
    db.add(lbl)
    db.commit()
    db.refresh(lbl)
    return lbl

@app.post("/api/labels/export-pdf")
def export_pdf_sheet(req: SheetPdfSchema):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4
    margin = 10 * mm
    gap = 3 * mm
    lw = req.label_width_mm * mm
    lh = req.label_height_mm * mm

    cols = max(1, int((page_w - 2 * margin + gap) / (lw + gap)))
    rows = max(1, int((page_h - 2 * margin + gap) / (lh + gap)))

    col_idx, row_idx = 0, 0
    for itm in req.items:
        x = margin + col_idx * (lw + gap)
        y = page_h - (margin + (row_idx + 1) * lh + row_idx * gap)

        c.setStrokeColorRGB(0.85, 0.85, 0.85)
        c.setLineWidth(0.3)
        c.rect(x, y, lw, lh)

        c.setFillColorRGB(0.2, 0.2, 0.2)
        c.setFont("Helvetica-Bold", 7)
        c.drawString(x + 2 * mm, y + lh - 3.5 * mm, str(itm.get("biz", "LabelForge"))[:28])

        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(x + 2 * mm, y + lh - 7 * mm, str(itm.get("title", ""))[:28])

        c.setFont("Helvetica", 6.5)
        price_text = f"MRP: {itm.get('price', '')}" if itm.get('price') else ""
        sku_text = f"SKU: {itm.get('sku', '')}" if itm.get('sku') else ""
        combined = f"{price_text}  {sku_text}".strip()
        if combined:
            c.drawString(x + 2 * mm, y + lh - 10 * mm, combined[:34])

        if itm.get("svg"):
            try:
                svg_buf = io.BytesIO(itm["svg"].encode('utf-8'))
                draw = svg2rlg(svg_buf)
                if draw:
                    sx = (lw - 4 * mm) / draw.width
                    sy = (lh - 12 * mm) / draw.height
                    sc = min(sx, sy)
                    draw.scale(sc, sc)
                    renderPDF.draw(draw, c, x + 2 * mm, y + 1.5 * mm)
            except Exception:
                pass

        col_idx += 1
        if col_idx >= cols:
            col_idx = 0
            row_idx += 1
            if row_idx >= rows:
                c.showPage()
                row_idx = 0

    c.save()
    return Response(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=labelforge_labels.pdf"}
    )

@app.get("/api/admin/metrics")
def admin_metrics(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin credentials required")
    users_cnt = db.query(User).count()
    biz_cnt = db.query(Business).count()
    prod_cnt = db.query(Product).count()
    lbls_cnt = db.query(SavedLabel).count()
    users = db.query(User).all()
    setting = db.query(SystemSetting).filter(SystemSetting.key == "pricing_config").first()
    pricing = json.loads(setting.value) if setting else {}
    return {
        "metrics": {"users": users_cnt, "businesses": biz_cnt, "products": prod_cnt, "labels": lbls_cnt},
        "users": [{"id": u.id, "email": u.email, "name": u.full_name, "role": u.role, "suspended": u.is_suspended, "business": u.business.name if u.business else "N/A"} for u in users],
        "pricing": pricing
    }

@app.post("/api/admin/toggle-suspend/{target_user_id}")
def toggle_suspend(target_user_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin credentials required")
    target = db.query(User).filter(User.id == target_user_id).first()
    if not target or target.role == "superadmin":
        raise HTTPException(status_code=400, detail="Cannot alter superadmin user")
    target.is_suspended = not target.is_suspended
    db.commit()
    return {"status": "updated", "is_suspended": target.is_suspended}

# ==========================================
# 6. RELIABLE SPA FRONTEND
# ==========================================
HTML_CONTENT = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>LabelForge — Enterprise Barcode & Label Platform</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    .stripe-pattern { background: repeating-linear-gradient(90deg, #000, #000 2px, transparent 2px, transparent 4px); }
  </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen">

  <div id="appRoot">
    <div class="p-12 text-center text-slate-400">Loading LabelForge SaaS platform...</div>
  </div>

  <script>
    const state = {
      view: 'landing',
      token: localStorage.getItem('token') || null,
      user: JSON.parse(localStorage.getItem('user') || 'null'),
      authMode: 'login',
      authStep: 1,
      authData: { full_name: '', email: '', phone: '', password: '', business_name: '', plan: 'free' },
      products: [],
      pricing: { free_price: 0, business_price: 799, professional_price: 1999, symbol: '₹' },
      selectedProductId: null,
      barcodeForm: { symbology: 'ean13', value: '8901234567890', text: true },
      currentSvg: '',
      labelSize: { width: 50, height: 25 },
      adminData: null
    };

    function setView(v) {
      state.view = v;
      render();
      window.scrollTo(0, 0);
      if (v === 'products') loadProducts();
      if (v === 'barcodes') loadBarcode();
      if (v === 'admin') loadAdmin();
    }

    async function api(url, method = 'GET', data = null) {
      const headers = { 'Content-Type': 'application/json' };
      if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
      const res = await fetch(url, { method, headers, body: data ? JSON.stringify(data) : null });
      if (res.status === 401 || res.status === 403) {
        if (state.token) {
          state.token = null;
          state.user = null;
          localStorage.clear();
          setView('landing');
        }
      }
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Request failed' }));
        throw new Error(err.detail || 'Error');
      }
      return res;
    }

    async function loadProducts() {
      try {
        const res = await (await api('/api/products')).json();
        state.products = res || [];
        render();
      } catch(e) { console.error(e); }
    }

    async function loadBarcode() {
      try {
        const q = new URLSearchParams({
          symbology: state.barcodeForm.symbology,
          value: state.barcodeForm.value,
          text: state.barcodeForm.text
        });
        const res = await fetch('/api/barcodes/render?' + q.toString(), { method: 'POST' });
        const errEl = document.getElementById('bcErr');
        if (!res.ok) {
          const err = await res.json();
          if (errEl) errEl.innerText = err.detail;
          return;
        }
        if (errEl) errEl.innerText = '';
        state.currentSvg = await res.text();
        const box = document.getElementById('bcBox');
        if (box) box.innerHTML = state.currentSvg;
      } catch(e) { console.error(e); }
    }

    async function handleLogin(e) {
      e.preventDefault();
      try {
        const email = document.getElementById('logEmail').value;
        const password = document.getElementById('logPass').value;
        const res = await (await api('/api/auth/login', 'POST', { email, password })).json();
        state.token = res.token;
        state.user = res.user;
        localStorage.setItem('token', res.token);
        localStorage.setItem('user', JSON.stringify(res.user));
        setView(res.user.role === 'superadmin' ? 'admin' : 'dashboard');
      } catch(err) { alert(err.message); }
    }

    async function handleRegister(e) {
      e.preventDefault();
      try {
        const res = await (await api('/api/auth/register', 'POST', state.authData)).json();
        state.token = res.token;
        state.user = res.user;
        localStorage.setItem('token', res.token);
        localStorage.setItem('user', JSON.stringify(res.user));
        alert('Welcome! Your commercial workspace is initialized.');
        setView('dashboard');
      } catch(err) { alert(err.message); }
    }

    async function saveProduct(e) {
      e.preventDefault();
      try {
        const body = {
          name: document.getElementById('pn').value,
          sku: document.getElementById('ps').value || null,
          barcode: document.getElementById('pb').value,
          barcode_type: document.getElementById('pt').value,
          mrp: parseFloat(document.getElementById('pm').value) || 0.0,
          selling_price: parseFloat(document.getElementById('psp').value) || 0.0,
          category: document.getElementById('pc').value
        };
        await api('/api/products', 'POST', body);
        alert('Product added to central database!');
        document.getElementById('modal').classList.add('hidden');
        loadProducts();
      } catch(err) { alert(err.message); }
    }

    async function exportPdf() {
      try {
        const items = state.products.length > 0 ? state.products.map(p => ({
          biz: state.user?.business_name || 'LabelForge Demo',
          title: p.name,
          price: '₹' + p.mrp,
          sku: p.sku,
          svg: state.currentSvg
        })) : [{
          biz: state.user?.business_name || 'LabelForge Demo',
          title: 'Sample Product Label',
          price: '₹499.00',
          sku: 'PRD-000001',
          svg: state.currentSvg
        }];

        const res = await api('/api/labels/export-pdf', 'POST', {
          items: items,
          label_width_mm: state.labelSize.width,
          label_height_mm: state.labelSize.height
        });
        const blob = await res.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'labels_' + Date.now() + '.pdf';
        a.click();
      } catch(err) { alert(err.message); }
    }

    async function loadAdmin() {
      try {
        const res = await (await api('/api/admin/metrics')).json();
        state.adminData = res;
        render();
      } catch(err) { alert(err.message); }
    }

    async function toggleSuspend(uid) {
      try {
        await api('/api/admin/toggle-suspend/' + uid, 'POST');
        loadAdmin();
      } catch(err) { alert(err.message); }
    }

    // Components
    function renderNav() {
      return `
        <header class="border-b border-slate-800 bg-slate-950/90 sticky top-0 z-50">
          <div class="max-w-7xl mx-auto px-6 h-16 flex items-center justify-between">
            <div class="flex items-center gap-3 cursor-pointer" onclick="setView('${state.token ? 'dashboard' : 'landing'}')">
              <div class="w-9 h-9 rounded-xl bg-indigo-600 flex items-center justify-center font-black text-white shadow-lg shadow-indigo-600/30">LF</div>
              <div>
                <span class="font-bold text-lg text-white block leading-tight">LabelForge</span>
                <span class="text-[10px] text-indigo-400 uppercase font-semibold">Commercial SaaS</span>
              </div>
            </div>

            <nav class="hidden md:flex items-center gap-6 text-sm font-medium text-slate-300">
              ${!state.token ? `
                <a href="#features" class="hover:text-white">Features</a>
                <a href="#barcode-module" class="hover:text-white">Barcode Studio</a>
                <a href="#label-module" class="hover:text-white">Label Designer</a>
                <a href="#pricing" class="hover:text-white">Pricing</a>
                <a href="#founder" class="hover:text-white">Founder</a>
              ` : `
                <button onclick="setView('dashboard')" class="${state.view === 'dashboard' ? 'text-indigo-400 font-bold' : 'hover:text-white'}">Dashboard</button>
                <button onclick="setView('products')" class="${state.view === 'products' ? 'text-indigo-400 font-bold' : 'hover:text-white'}">Products Central</button>
                <button onclick="setView('barcodes')" class="${state.view === 'barcodes' ? 'text-indigo-400 font-bold' : 'hover:text-white'}">Barcode Studio</button>
                <button onclick="setView('labels')" class="${state.view === 'labels' ? 'text-indigo-400 font-bold' : 'hover:text-white'}">Label Canvas</button>
                ${state.user?.role === 'superadmin' ? `
                  <button onclick="setView('admin')" class="${state.view === 'admin' ? 'text-amber-400 font-bold' : 'text-amber-500 hover:text-white'}">Superadmin</button>
                ` : ''}
              `}
            </nav>

            <div class="flex items-center gap-3">
              ${!state.token ? `
                <button onclick="setView('login')" class="text-xs font-semibold px-4 py-2 hover:text-white text-slate-300">Login</button>
                <button onclick="setView('register')" class="text-xs font-semibold px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg shadow-md shadow-indigo-600/30">Start Free</button>
              ` : `
                <span class="text-xs text-slate-400">${state.user?.business_name || 'Business'}</span>
                <button onclick="state.token=null; state.user=null; localStorage.clear(); setView('landing')" class="text-xs px-3 py-1 bg-red-950/50 border border-red-800 text-red-300 rounded-lg">Sign Out</button>
              `}
            </div>
          </div>
        </header>
      `;
    }

    function renderLanding() {
      const sym = state.pricing.symbol;
      return `
        <div class="max-w-7xl mx-auto px-6 pt-20 pb-16 text-center">
          <div class="inline-block px-4 py-1.5 rounded-full border border-indigo-500/30 bg-indigo-500/10 text-indigo-400 text-xs font-semibold uppercase mb-6">
            Single Source of Truth for Packaging & Barcodes
          </div>
          <h1 class="text-4xl sm:text-6xl font-black text-white tracking-tight max-w-4xl mx-auto leading-tight">
            Professional Barcode Generation & Product Label Design — Made Simple.
          </h1>
          <p class="mt-6 text-slate-400 text-base sm:text-lg max-w-2xl mx-auto">
            Create, manage and print professional product barcodes and commercial labels from one powerful platform. Enter your product once and automatically use it across barcodes, labels, and bulk print sheets.
          </p>
          <div class="mt-8 flex justify-center gap-4">
            <button onclick="setView('register')" class="px-8 py-3.5 bg-indigo-600 hover:bg-indigo-500 font-bold text-sm text-white rounded-xl shadow-lg shadow-indigo-600/30">
              Start Free Platform
            </button>
            <button onclick="setView('barcodes')" class="px-8 py-3.5 bg-slate-900 border border-slate-800 font-bold text-sm text-slate-300 rounded-xl hover:bg-slate-800">
              Open Barcode Studio
            </button>
          </div>

          <!-- Problem Section -->
          <div class="mt-24 p-8 rounded-3xl bg-slate-900/50 border border-slate-800 text-left">
            <span class="text-xs font-bold uppercase tracking-widest text-red-400">Stop Creating Product Labels Manually</span>
            <h2 class="text-2xl font-bold text-white mt-1">One Product. Everything Connected.</h2>
            <p class="text-sm text-slate-400 mt-2">
              Businesses repeatedly enter product titles, SKUs, barcode numbers, MRPs, GSTINs, and batch numbers across multiple programs. With LabelForge, you store your product specifications once in your central database, and every barcode and sticker pulls from it automatically.
            </p>
          </div>

          <!-- Modules Showcase -->
          <div id="features" class="mt-16 grid grid-cols-1 md:grid-cols-3 gap-6 text-left">
            <div class="p-6 bg-slate-900 border border-slate-800 rounded-2xl">
              <h3 class="font-bold text-white text-base">Mathematical Barcode Engine</h3>
              <p class="text-xs text-slate-400 mt-2">GS1 Modulo-10 verified check-digit generation for EAN-13, UPC-A, Code 128, and QR Codes in pure vector SVG.</p>
            </div>
            <div class="p-6 bg-slate-900 border border-slate-800 rounded-2xl">
              <h3 class="font-bold text-white text-base">Two-Panel Label Designer</h3>
              <p class="text-xs text-slate-400 mt-2">Select a product to instantly populate your company name, retail price, MRP, and barcode onto a 50×25mm canvas.</p>
            </div>
            <div class="p-6 bg-slate-900 border border-slate-800 rounded-2xl">
              <h3 class="font-bold text-white text-base">Thermal & Sheet PDF Export</h3>
              <p class="text-xs text-slate-400 mt-2">Direct A4 sticker layout with cut guides, zero-scaling vector rendering, and bulk printer compatibility.</p>
            </div>
          </div>

          <!-- Pricing Section -->
          <div id="pricing" class="mt-24 text-left">
            <div class="text-center mb-10">
              <span class="text-xs font-bold uppercase text-indigo-400">Predictable Subscriptions</span>
              <h2 class="text-3xl font-black text-white mt-1">SaaS Pricing Plans</h2>
            </div>
            <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
              <div class="p-8 rounded-3xl bg-slate-900 border border-slate-800 flex flex-col justify-between">
                <div>
                  <h4 class="font-bold text-slate-400 text-xs uppercase">Free Plan</h4>
                  <p class="text-3xl font-black text-white mt-2">${sym}0</p>
                  <p class="text-xs text-slate-400 mt-1">Up to 15 Products • Basic Barcodes & Labels</p>
                </div>
                <button onclick="setView('register')" class="mt-6 w-full py-2.5 bg-slate-800 hover:bg-slate-700 text-xs font-bold rounded-xl">Get Started Free</button>
              </div>
              <div class="p-8 rounded-3xl bg-slate-900 border-2 border-indigo-500 flex flex-col justify-between shadow-xl shadow-indigo-600/20">
                <div>
                  <span class="text-xs font-bold text-indigo-400 uppercase">Most Popular</span>
                  <h4 class="font-bold text-white text-lg mt-1">Business Plan</h4>
                  <p class="text-3xl font-black text-white mt-2">${sym}${state.pricing.business_price}<span class="text-xs text-slate-500 font-normal"> / month</span></p>
                  <p class="text-xs text-slate-400 mt-1">2,000 SKUs • Unlimited Barcodes • SVG & PDF Export</p>
                </div>
                <button onclick="setView('register')" class="mt-6 w-full py-2.5 bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-bold rounded-xl shadow-md">Start Business Plan</button>
              </div>
              <div class="p-8 rounded-3xl bg-slate-900 border border-slate-800 flex flex-col justify-between">
                <div>
                  <h4 class="font-bold text-slate-400 text-xs uppercase">Professional</h4>
                  <p class="text-3xl font-black text-white mt-2">${sym}${state.pricing.professional_price}<span class="text-xs text-slate-500 font-normal"> / month</span></p>
                  <p class="text-xs text-slate-400 mt-1">Unlimited Products • Team Roles • Developer API</p>
                </div>
                <button onclick="setView('register')" class="mt-6 w-full py-2.5 bg-slate-800 hover:bg-slate-700 text-xs font-bold rounded-xl">Start Professional</button>
              </div>
            </div>
          </div>

          <!-- Founder Section -->
          <div id="founder" class="mt-24 p-8 sm:p-12 rounded-3xl bg-slate-900/60 border border-slate-800 text-left max-w-4xl mx-auto flex flex-col md:flex-row items-center gap-8 shadow-xl">
            <div class="w-24 h-24 rounded-2xl bg-indigo-600 flex items-center justify-center font-black text-3xl text-white shrink-0">SC</div>
            <div>
              <span class="text-xs font-bold text-indigo-400 uppercase tracking-wider block">Meet the Founder</span>
              <h3 class="text-2xl font-bold text-white mt-1">Siddharth Chopra</h3>
              <p class="text-xs text-slate-400 font-medium">Founder & Entrepreneur</p>
              <p class="mt-3 text-slate-300 text-sm leading-relaxed italic border-l-2 border-indigo-500 pl-4">
                "Siddharth Chopra is a young entrepreneur with a passion for technology, software and building practical digital products. His vision is to simplify everyday business operations through accessible, modern software that combines powerful functionality with an easy-to-use experience."
              </p>
            </div>
          </div>

          <footer class="mt-20 pt-8 border-t border-slate-900 text-xs text-slate-500">
            © 2026 LabelForge SaaS. Enter your product information once. Use it everywhere.
          </footer>
        </div>
      `;
    }

    function renderAuth() {
      if (state.view === 'login') {
        return `
          <div class="max-w-md mx-auto px-6 py-20">
            <div class="bg-slate-900 border border-slate-800 p-8 rounded-3xl shadow-xl">
              <h2 class="text-2xl font-bold text-white mb-1">Sign In</h2>
              <p class="text-xs text-slate-400 mb-6">Enter your workspace email and password</p>
              <form onsubmit="handleLogin(event)" class="space-y-4">
                <div>
                  <label class="block text-xs font-semibold text-slate-400 mb-1">Email</label>
                  <input id="logEmail" type="email" required class="w-full bg-slate-950 border border-slate-800 rounded-xl p-2.5 text-xs text-white" placeholder="admin@nameofweb" />
                </div>
                <div>
                  <label class="block text-xs font-semibold text-slate-400 mb-1">Password</label>
                  <input id="logPass" type="password" required class="w-full bg-slate-950 border border-slate-800 rounded-xl p-2.5 text-xs text-white" placeholder="••••••••" />
                </div>
                <button type="submit" class="w-full py-3 bg-indigo-600 hover:bg-indigo-500 text-white font-bold text-xs rounded-xl shadow-lg shadow-indigo-600/30">Sign In</button>
              </form>
              <p class="text-xs text-center text-slate-500 mt-4">Don't have an account? <a href="#" onclick="setView('register')" class="text-indigo-400 underline">Register</a></p>
            </div>
          </div>
        `;
      }

      return `
        <div class="max-w-lg mx-auto px-6 py-16">
          <div class="bg-slate-900 border border-slate-800 p-8 rounded-3xl shadow-xl">
            <h2 class="text-2xl font-bold text-white mb-1">Create Workspace</h2>
            <p class="text-xs text-slate-400 mb-6">Set up your multi-tenant business account</p>
            <form onsubmit="handleRegister(event)" class="space-y-4">
              <div>
                <label class="block text-xs font-semibold text-slate-400 mb-1">Full Name</label>
                <input oninput="state.authData.full_name=this.value" required class="w-full bg-slate-950 border border-slate-800 rounded-xl p-2.5 text-xs text-white" placeholder="Siddharth Chopra" />
              </div>
              <div>
                <label class="block text-xs font-semibold text-slate-400 mb-1">Work Email</label>
                <input type="email" oninput="state.authData.email=this.value" required class="w-full bg-slate-950 border border-slate-800 rounded-xl p-2.5 text-xs text-white" placeholder="siddharth@business.com" />
              </div>
              <div>
                <label class="block text-xs font-semibold text-slate-400 mb-1">Business Name</label>
                <input oninput="state.authData.business_name=this.value" required class="w-full bg-slate-950 border border-slate-800 rounded-xl p-2.5 text-xs text-white" placeholder="Apex Consumer Goods" />
              </div>
              <div>
                <label class="block text-xs font-semibold text-slate-400 mb-1">Password</label>
                <input type="password" oninput="state.authData.password=this.value" required class="w-full bg-slate-950 border border-slate-800 rounded-xl p-2.5 text-xs text-white" placeholder="••••••••" />
              </div>
              <div>
                <label class="block text-xs font-semibold text-slate-400 mb-1">Workspace Plan</label>
                <select onchange="state.authData.plan=this.value" class="w-full bg-slate-950 border border-slate-800 rounded-xl p-2.5 text-xs text-white">
                  <option value="free">Free Starter (₹0)</option>
                  <option value="business">Business Plan (₹799/mo)</option>
                  <option value="professional">Professional Tier (₹1,999/mo)</option>
                </select>
              </div>
              <button type="submit" class="w-full py-3 bg-indigo-600 hover:bg-indigo-500 text-white font-bold text-xs rounded-xl shadow-lg shadow-indigo-600/30">Create Workspace</button>
            </form>
          </div>
        </div>
      `;
    }

    function renderDashboard() {
      return `
        <div class="max-w-7xl mx-auto px-6 py-10">
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-8">
            <div>
              <h2 class="text-3xl font-black text-white">Commercial Workspace</h2>
              <p class="text-xs text-slate-400 mt-1">Tenant: <span class="text-indigo-400 font-bold">${state.user?.business_name || 'My Business'}</span> • Plan: <span class="uppercase font-mono">${state.user?.plan || 'free'}</span></p>
            </div>
            <div class="flex gap-3">
              <button onclick="document.getElementById('modal').classList.remove('hidden')" class="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 rounded-xl text-xs font-bold">+ Add Product</button>
              <button onclick="setView('labels')" class="px-4 py-2 bg-slate-800 hover:bg-slate-700 rounded-xl text-xs font-bold">Designer Studio</button>
            </div>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
            <div onclick="setView('products')" class="p-6 bg-slate-900 border border-slate-800 rounded-2xl cursor-pointer hover:border-indigo-500">
              <h3 class="font-bold text-white text-base">Product Central Database</h3>
              <p class="text-xs text-slate-400 mt-1">Manage SKUs, retail pricing, MRP, barcodes, and tax fields.</p>
            </div>
            <div onclick="setView('barcodes')" class="p-6 bg-slate-900 border border-slate-800 rounded-2xl cursor-pointer hover:border-cyan-500">
              <h3 class="font-bold text-white text-base">Barcode Generator</h3>
              <p class="text-xs text-slate-400 mt-1">Select any product to instantly populate its symbology and check digit.</p>
            </div>
            <div onclick="setView('labels')" class="p-6 bg-slate-900 border border-slate-800 rounded-2xl cursor-pointer hover:border-emerald-500">
              <h3 class="font-bold text-white text-base">Commercial Label Canvas</h3>
              <p class="text-xs text-slate-400 mt-1">Design packaging labels with real-time automatic data binding.</p>
            </div>
          </div>
        </div>
      `;
    }

    function renderProducts() {
      return `
        <div class="max-w-7xl mx-auto px-6 py-10">
          <div class="flex items-center justify-between mb-6">
            <div>
              <h2 class="text-2xl font-bold text-white">Central Product Database</h2>
              <p class="text-xs text-slate-400">Single source of truth for all barcodes and packaging stickers</p>
            </div>
            <button onclick="document.getElementById('modal').classList.remove('hidden')" class="px-4 py-2 bg-indigo-600 rounded-xl text-xs font-bold">+ Add Product</button>
          </div>

          <div class="bg-slate-900 border border-slate-800 rounded-2xl overflow-hidden">
            <table class="w-full text-left text-xs text-slate-300">
              <thead class="bg-slate-950 border-b border-slate-800 text-slate-400 font-bold uppercase">
                <tr>
                  <th class="p-4">SKU</th>
                  <th class="p-4">Product Name</th>
                  <th class="p-4">Barcode</th>
                  <th class="p-4">MRP (₹)</th>
                  <th class="p-4">Selling Price (₹)</th>
                  <th class="p-4 text-right">Actions</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-800/60">
                ${state.products.length === 0 ? `
                  <tr><td colspan="6" class="p-8 text-center text-slate-500">No products registered yet. Click '+ Add Product' above.</td></tr>
                ` : state.products.map(p => `
                  <tr>
                    <td class="p-4 font-mono font-bold text-white">${p.sku}</td>
                    <td class="p-4 font-semibold text-white">${p.name}</td>
                    <td class="p-4 font-mono">${p.barcode} (${p.barcode_type})</td>
                    <td class="p-4">₹${(p.mrp || 0).toFixed(2)}</td>
                    <td class="p-4">₹${(p.selling_price || 0).toFixed(2)}</td>
                    <td class="p-4 text-right">
                      <button onclick="state.selectedProductId=${p.id}; state.barcodeForm.symbology='${p.barcode_type}'; state.barcodeForm.value='${p.barcode}'; setView('barcodes');" class="text-indigo-400 hover:underline mr-3">Barcode</button>
                      <button onclick="state.selectedProductId=${p.id}; setView('labels');" class="text-emerald-400 hover:underline">Label</button>
                    </td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
        </div>
      `;
    }

    function renderBarcodes() {
      return `
        <div class="max-w-6xl mx-auto px-6 py-10">
          <h2 class="text-2xl font-bold text-white mb-1">Smart Barcode Studio</h2>
          <p class="text-xs text-slate-400 mb-6">Select an existing product or enter manual payload</p>

          <div class="grid grid-cols-1 md:grid-cols-2 gap-8">
            <div class="bg-slate-900 border border-slate-800 p-6 rounded-3xl space-y-4">
              <div>
                <label class="block text-xs font-semibold text-slate-400 mb-1">Select Existing Product</label>
                <select onchange="
                  const p = state.products.find(x => x.id == this.value);
                  if(p) {
                    state.selectedProductId = p.id;
                    state.barcodeForm.symbology = p.barcode_type;
                    state.barcodeForm.value = p.barcode;
                    loadBarcode();
                    render();
                  }
                " class="w-full bg-slate-950 border border-slate-800 rounded-xl p-2.5 text-xs text-white">
                  <option value="">-- Choose Product --</option>
                  ${state.products.map(p => `
                    <option value="${p.id}" ${state.selectedProductId === p.id ? 'selected' : ''}>${p.name} (${p.sku})</option>
                  `).join('')}
                </select>
              </div>

              <div>
                <label class="block text-xs font-semibold text-slate-400 mb-1">Symbology</label>
                <select id="bcs" onchange="state.barcodeForm.symbology=this.value; loadBarcode();" class="w-full bg-slate-950 border border-slate-800 rounded-xl p-2.5 text-xs text-white">
                  <option value="ean13" ${state.barcodeForm.symbology==='ean13'?'selected':''}>EAN-13</option>
                  <option value="code128" ${state.barcodeForm.symbology==='code128'?'selected':''}>Code 128</option>
                  <option value="code39" ${state.barcodeForm.symbology==='code39'?'selected':''}>Code 39</option>
                  <option value="qrcode" ${state.barcodeForm.symbology==='qrcode'?'selected':''}>QR Code</option>
                </select>
              </div>

              <div>
                <label class="block text-xs font-semibold text-slate-400 mb-1">Barcode Number</label>
                <input id="bcv" value="${state.barcodeForm.value}" oninput="state.barcodeForm.value=this.value; loadBarcode();" class="w-full bg-slate-950 border border-slate-800 rounded-xl p-2.5 text-xs text-white font-mono" />
                <p id="bcErr" class="text-xs text-red-400 mt-1 font-semibold"></p>
              </div>

              <div class="pt-4 flex gap-3">
                <button onclick="exportPdf()" class="flex-1 py-2.5 bg-indigo-600 hover:bg-indigo-500 font-bold text-xs rounded-xl">Export PDF Sheet</button>
              </div>
            </div>

            <div class="bg-slate-900 border border-slate-800 p-6 rounded-3xl flex flex-col items-center justify-center min-h-[300px]">
              <span class="text-xs font-bold text-slate-500 uppercase mb-4">Vector Barcode Preview</span>
              <div id="bcBox" class="bg-white p-6 rounded-2xl border border-slate-300 w-full max-w-xs flex items-center justify-center text-slate-950 shadow-xl">
                ${state.currentSvg || '<span class="text-xs text-slate-400">Rendering barcode...</span>'}
              </div>
            </div>
          </div>
        </div>
      `;
    }

    function renderLabels() {
      const p = state.products.find(x => x.id == state.selectedProductId);
      const biz = state.user?.business_name || 'Apex Consumer Goods';
      const title = p ? p.name : 'Organic Basmati Rice 5kg';
      const mrp = p ? 'MRP: ₹' + (p.mrp || 0).toFixed(2) : 'MRP: ₹650.00';
      const sp = p ? 'SP: ₹' + (p.selling_price || 0).toFixed(2) : 'SP: ₹599.00';
      const code = p ? p.barcode : '8901234567890';

      return `
        <div class="max-w-6xl mx-auto px-6 py-10">
          <div class="flex items-center justify-between mb-6">
            <div>
              <h2 class="text-2xl font-bold text-white">Commercial Label Designer</h2>
              <p class="text-xs text-slate-400">Two-panel live canvas linked to your product catalog</p>
            </div>
            <button onclick="exportPdf()" class="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 font-bold text-xs rounded-xl">Export PDF Sheet</button>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-3 gap-8">
            <div class="bg-slate-900 border border-slate-800 p-6 rounded-3xl space-y-4">
              <div>
                <label class="block text-xs font-semibold text-slate-400 mb-1">Select Product to Auto-Fill</label>
                <select onchange="
                  state.selectedProductId = this.value ? parseInt(this.value) : null;
                  render();
                " class="w-full bg-slate-950 border border-slate-800 rounded-xl p-2.5 text-xs text-white">
                  <option value="">-- Choose Product --</option>
                  ${state.products.map(x => `
                    <option value="${x.id}" ${state.selectedProductId === x.id ? 'selected' : ''}>${x.name}</option>
                  `).join('')}
                </select>
              </div>
              <div class="text-xs space-y-2 text-slate-400 pt-2 border-t border-slate-800">
                <p><span class="text-slate-500">Business:</span> <span class="font-bold text-white">${biz}</span></p>
                <p><span class="text-slate-500">Product:</span> <span class="font-bold text-white">${title}</span></p>
                <p><span class="text-slate-500">Pricing:</span> <span class="font-bold text-emerald-400">${mrp} | ${sp}</span></p>
                <p><span class="text-slate-500">Barcode:</span> <span class="font-mono text-white">${code}</span></p>
              </div>
            </div>

            <div class="md:col-span-2 bg-slate-900 border border-slate-800 p-8 rounded-3xl flex flex-col items-center justify-center min-h-[350px]">
              <span class="text-xs font-bold text-slate-500 uppercase mb-4">50mm × 25mm Thermal Label Surface</span>
              <div style="width: 360px; height: 180px;" class="bg-white rounded-md p-4 text-slate-950 shadow-2xl relative border-2 border-dashed border-indigo-400 select-none">
                <p class="text-[10px] font-extrabold uppercase text-slate-600">${biz}</p>
                <p class="text-xs font-black mt-1">${title}</p>
                <p class="text-[10px] font-semibold text-slate-700 mt-1">${mrp} | ${sp}</p>
                <div class="w-full h-8 stripe-pattern mt-3"></div>
                <p class="text-[9px] font-mono text-center mt-0.5">${code}</p>
              </div>
            </div>
          </div>
        </div>
      `;
    }

    function renderAdmin() {
      if (!state.adminData) return '<div class="p-16 text-center text-slate-400">Loading admin metrics...</div>';
      const m = state.adminData.metrics || {};
      return `
        <div class="max-w-7xl mx-auto px-6 py-10">
          <h2 class="text-3xl font-black text-amber-400 mb-1">Super Administrator Headquarters</h2>
          <p class="text-xs text-slate-400 mb-8">Multi-tenant moderation and platform controls</p>

          <div class="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
            <div class="p-6 rounded-2xl bg-slate-900 border border-slate-800">
              <span class="text-xs font-bold text-slate-400 uppercase">Businesses</span>
              <p class="text-3xl font-extrabold text-white mt-2">${m.businesses || 0}</p>
            </div>
            <div class="p-6 rounded-2xl bg-slate-900 border border-slate-800">
              <span class="text-xs font-bold text-slate-400 uppercase">Users</span>
              <p class="text-3xl font-extrabold text-indigo-400 mt-2">${m.users || 0}</p>
            </div>
            <div class="p-6 rounded-2xl bg-slate-900 border border-slate-800">
              <span class="text-xs font-bold text-slate-400 uppercase">Catalog Products</span>
              <p class="text-3xl font-extrabold text-emerald-400 mt-2">${m.products || 0}</p>
            </div>
            <div class="p-6 rounded-2xl bg-slate-900 border border-slate-800">
              <span class="text-xs font-bold text-slate-400 uppercase">Labels Saved</span>
              <p class="text-3xl font-extrabold text-cyan-400 mt-2">${m.labels || 0}</p>
            </div>
          </div>

          <div class="bg-slate-900 border border-slate-800 rounded-3xl overflow-hidden">
            <div class="p-5 font-bold text-white text-sm border-b border-slate-800">Tenant Users</div>
            <table class="w-full text-left text-xs text-slate-300">
              <thead class="bg-slate-950 border-b border-slate-800 text-slate-400 uppercase font-semibold">
                <tr>
                  <th class="p-4">Name</th>
                  <th class="p-4">Email</th>
                  <th class="p-4">Business</th>
                  <th class="p-4">Status</th>
                  <th class="p-4 text-right">Action</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-800/60">
                ${state.adminData.users.map(u => `
                  <tr>
                    <td class="p-4 font-bold text-white">${u.name}</td>
                    <td class="p-4 font-mono">${u.email}</td>
                    <td class="p-4">${u.business}</td>
                    <td class="p-4">
                      <span class="px-2 py-0.5 rounded ${u.suspended ? 'bg-red-950 text-red-400' : 'bg-emerald-950 text-emerald-400'}">
                        ${u.suspended ? 'Suspended' : 'Active'}
                      </span>
                    </td>
                    <td class="p-4 text-right">
                      ${u.role !== 'superadmin' ? `
                        <button onclick="toggleSuspend(${u.id})" class="px-3 py-1 bg-slate-800 rounded text-slate-300 font-bold">
                          ${u.suspended ? 'Unsuspend' : 'Suspend'}
                        </button>
                      ` : '<span class="text-slate-600 italic">Protected</span>'}
                    </td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
        </div>
      `;
    }

    function renderModal() {
      return `
        <div id="modal" class="hidden fixed inset-0 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center p-4 z-50">
          <div class="bg-slate-900 border border-slate-800 p-6 rounded-3xl max-w-md w-full shadow-2xl">
            <h3 class="text-lg font-bold text-white mb-4">Add Product to Central Database</h3>
            <form onsubmit="saveProduct(event)" class="space-y-3">
              <div>
                <label class="block text-xs font-semibold text-slate-400 mb-1">Product Title</label>
                <input id="pn" required class="w-full bg-slate-950 border border-slate-800 rounded-xl p-2 text-xs text-white" placeholder="Basmati Rice 5kg" />
              </div>
              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label class="block text-xs font-semibold text-slate-400 mb-1">SKU Code</label>
                  <input id="ps" class="w-full bg-slate-950 border border-slate-800 rounded-xl p-2 text-xs text-white font-mono" placeholder="PRD-000001" />
                </div>
                <div>
                  <label class="block text-xs font-semibold text-slate-400 mb-1">Barcode Payload</label>
                  <input id="pb" required class="w-full bg-slate-950 border border-slate-800 rounded-xl p-2 text-xs text-white font-mono" placeholder="8901234567890" />
                </div>
              </div>
              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label class="block text-xs font-semibold text-slate-400 mb-1">Symbology</label>
                  <select id="pt" class="w-full bg-slate-950 border border-slate-800 rounded-xl p-2 text-xs text-white">
                    <option value="ean13">EAN-13</option>
                    <option value="code128">Code 128</option>
                    <option value="code39">Code 39</option>
                    <option value="qrcode">QR Code</option>
                  </select>
                </div>
                <div>
                  <label class="block text-xs font-semibold text-slate-400 mb-1">Category</label>
                  <input id="pc" value="General" class="w-full bg-slate-950 border border-slate-800 rounded-xl p-2 text-xs text-white" />
                </div>
              </div>
              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label class="block text-xs font-semibold text-slate-400 mb-1">MRP Price (₹)</label>
                  <input id="pm" type="number" step="0.01" value="0.00" class="w-full bg-slate-950 border border-slate-800 rounded-xl p-2 text-xs text-white font-mono" />
                </div>
                <div>
                  <label class="block text-xs font-semibold text-slate-400 mb-1">Selling Price (₹)</label>
                  <input id="psp" type="number" step="0.01" value="0.00" class="w-full bg-slate-950 border border-slate-800 rounded-xl p-2 text-xs text-white font-mono" />
                </div>
              </div>
              <div class="pt-4 flex gap-3">
                <button type="button" onclick="document.getElementById('modal').classList.add('hidden')" class="w-1/3 py-2 bg-slate-800 rounded-xl text-xs font-bold">Cancel</button>
                <button type="submit" class="w-2/3 py-2 bg-indigo-600 hover:bg-indigo-500 rounded-xl text-xs font-bold text-white">Save Product</button>
              </div>
            </form>
          </div>
        </div>
      `;
    }

    function render() {
      const root = document.getElementById('appRoot');
      if (!root) return;
      let body = '';
      if (state.view === 'landing') body = renderLanding();
      else if (state.view === 'login' || state.view === 'register') body = renderAuth();
      else if (state.view === 'dashboard') body = renderDashboard();
      else if (state.view === 'products') body = renderProducts();
      else if (state.view === 'barcodes') body = renderBarcodes();
      else if (state.view === 'labels') body = renderLabels();
      else if (state.view === 'admin') body = renderAdmin();

      root.innerHTML = renderNav() + body + renderModal();
    }

    // Direct synchronous initial paint
    render();

    if (state.token && state.user) {
      if (state.user.role === 'superadmin') setView('admin');
      else setView('dashboard');
    }
  </script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
@app.get("/app", response_class=HTMLResponse)
def serve_ui():
    return HTMLResponse(content=HTML_CONTENT)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)