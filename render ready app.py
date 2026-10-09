from flask import Flask, request, redirect, render_template_string, session, url_for, send_from_directory
from dotenv import load_dotenv
import sqlite3
import requests
import os
import statistics
import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
import smtplib
import stripe
import re
from email.message import EmailMessage
from datetime import datetime
from urllib.parse import quote_plus
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

APP_FOLDER = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(APP_FOLDER, ".env")
load_dotenv(ENV_FILE, override=True)

# Local development uses the project folder. On Render, set DATA_DIR=/var/data
# and attach a persistent disk at /var/data.
DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", APP_FOLDER))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "flipfinder.db")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or "change-this-before-publishing"
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads", "iphone_comments")
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

stripe.api_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
STRIPE_WEBHOOK_SECRET = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
STRIPE_PRICE_PROMOTION = (os.getenv("STRIPE_PRICE_PROMOTION") or "").strip()
STRIPE_PRICES = {
    "weekly": os.getenv("STRIPE_PRICE_WEEKLY"),
    "monthly": os.getenv("STRIPE_PRICE_MONTHLY"),
    "yearly": os.getenv("STRIPE_PRICE_YEARLY"),
}

COUNTRIES = {
    "AU": {"name":"Australia","flag":"🇦🇺","currency":"AUD","symbol":"A$","language":"en","locale":"en-AU","rate":1.0,"default_location":"Nuriootpa, SA"},
    "NZ": {"name":"New Zealand","flag":"🇳🇿","currency":"NZD","symbol":"NZ$","language":"en","locale":"en-NZ","rate":1.09,"default_location":"Auckland, New Zealand"},
    "US": {"name":"United States","flag":"🇺🇸","currency":"USD","symbol":"US$","language":"en","locale":"en-US","rate":0.66,"default_location":"Los Angeles, CA"},
    "CA": {"name":"Canada","flag":"🇨🇦","currency":"CAD","symbol":"C$","language":"en","locale":"en-CA","rate":0.91,"default_location":"Toronto, ON"},
    "GB": {"name":"United Kingdom","flag":"🇬🇧","currency":"GBP","symbol":"£","language":"en","locale":"en-GB","rate":0.50,"default_location":"London, United Kingdom"},
    "DE": {"name":"Germany","flag":"🇩🇪","currency":"EUR","symbol":"€","language":"de","locale":"de-DE","rate":0.57,"default_location":"Berlin, Germany"},
    "FR": {"name":"France","flag":"🇫🇷","currency":"EUR","symbol":"€","language":"fr","locale":"fr-FR","rate":0.57,"default_location":"Paris, France"},
    "ES": {"name":"Spain","flag":"🇪🇸","currency":"EUR","symbol":"€","language":"es","locale":"es-ES","rate":0.57,"default_location":"Madrid, Spain"},
    "JP": {"name":"Japan","flag":"🇯🇵","currency":"JPY","symbol":"¥","language":"ja","locale":"ja-JP","rate":99.0,"default_location":"Tokyo, Japan"},
    "IN": {"name":"India","flag":"🇮🇳","currency":"INR","symbol":"₹","language":"hi","locale":"hi-IN","rate":58.5,"default_location":"Mumbai, India"},
    "SG": {"name":"Singapore","flag":"🇸🇬","currency":"SGD","symbol":"S$","language":"en","locale":"en-SG","rate":0.85,"default_location":"Singapore"},
    "AE": {"name":"United Arab Emirates","flag":"🇦🇪","currency":"AED","symbol":"د.إ","language":"ar","locale":"ar-AE","rate":2.42,"default_location":"Dubai, United Arab Emirates"},
}
LANGUAGES = {"en":"English","de":"Deutsch","fr":"Français","es":"Español","ja":"日本語","hi":"हिन्दी","ar":"العربية"}

def current_country_code():
    user = get_current_user() if session.get("user_id") else None
    code = (user["country_code"] if user and "country_code" in user.keys() else None) or session.get("country_code") or "AU"
    return code if code in COUNTRIES else "AU"

def current_search_location():
    user = get_current_user() if session.get("user_id") else None
    saved = (user["search_location"] if user and "search_location" in user.keys() else None) or session.get("search_location")
    return (saved or COUNTRIES[current_country_code()]["default_location"]).strip()

def current_language():
    user = get_current_user() if session.get("user_id") else None
    language = (user["language"] if user and "language" in user.keys() else None) or session.get("language")
    return language if language in LANGUAGES else COUNTRIES[current_country_code()]["language"]

def money(amount, source_currency="AUD"):
    try:
        amount=float(amount)
    except (TypeError, ValueError):
        return str(amount)
    country=COUNTRIES[current_country_code()]
    value=amount * country["rate"] if source_currency == "AUD" else amount
    decimals=0 if country["currency"]=="JPY" else 2
    return f'{country["symbol"]}{value:,.{decimals}f}'

def localize_aud_text(html):
    # Converts rendered A$ amounts for display only. Stripe Checkout remains authoritative.
    if current_country_code()=="AU":
        return html
    pattern=re.compile(r'A\$(\d+(?:,\d{3})*(?:\.\d+)?)')
    return pattern.sub(lambda m: money(m.group(1).replace(',',''), "AUD"), html)

app.jinja_env.filters["money"] = money

PHONE_VALUES = {
    "iPhone 11 64GB": 280,
    "iPhone 11 128GB": 330,
    "iPhone 12 64GB": 400,
    "iPhone 12 128GB": 450,
    "iPhone 13 128GB": 550,
    "iPhone 13 256GB": 620,
    "iPhone 14 128GB": 700,
    "iPhone 14 256GB": 770,
    "iPhone 15 128GB": 850,
    "iPhone 15 256GB": 930,
}

DEVICE_CONFIG = {
    "iphone": {"label": "iPhone", "ebay_category": "9355", "suffix": "unlocked smartphone"},
    "macbook": {"label": "MacBook", "ebay_category": "111422", "suffix": "Apple MacBook laptop"},
    "ipad": {"label": "iPad", "ebay_category": "171485", "suffix": "Apple iPad tablet"},
    "imac": {"label": "iMac", "ebay_category": "111418", "suffix": "Apple iMac computer"},
    "mac_mini": {"label": "Mac mini", "ebay_category": "111418", "suffix": "Apple Mac mini computer"},
    "apple_watch": {"label": "Apple Watch", "ebay_category": "178893", "suffix": "Apple Watch smartwatch"},
}

STYLE = """
<style>
* { box-sizing: border-box; }
body { margin:0; min-height:100vh; background:radial-gradient(circle at top,#172750,#080b14 60%); color:white; font-family:Arial,sans-serif; }
nav { position:sticky; top:0; z-index:50; display:flex; justify-content:space-between; align-items:center; padding:16px 6%; background:rgba(8,11,20,.88); border-bottom:1px solid rgba(105,163,255,.22); backdrop-filter:blur(18px); box-shadow:0 10px 30px rgba(0,0,0,.18); }
.logo { display:flex; align-items:center; gap:10px; color:#69a3ff; font-size:22px; font-weight:900; letter-spacing:1px; cursor:pointer; }
.logo::before { content:"F"; width:38px; height:38px; display:grid; place-items:center; border-radius:12px; color:white; background:linear-gradient(135deg,#347cff,#8a4dff); box-shadow:0 8px 22px rgba(88,85,255,.38); }
.nav-button { margin-left:6px; padding:10px 14px; border:1px solid transparent; border-radius:999px; background:transparent; color:white; cursor:pointer; font-weight:700; transition:.18s ease; }
.nav-button:hover { background:rgba(105,163,255,.13); border-color:rgba(105,163,255,.30); transform:translateY(-1px); }
.hero { position:relative; max-width:1080px; margin:38px auto 0; padding:105px 35px 80px; text-align:center; overflow:hidden; border:1px solid rgba(105,163,255,.18); border-radius:32px; background:linear-gradient(145deg,rgba(21,34,68,.90),rgba(10,14,27,.92)); box-shadow:0 30px 80px rgba(0,0,0,.35); }
.hero::before { content:""; position:absolute; width:420px; height:420px; left:-180px; top:-210px; border-radius:50%; background:rgba(52,124,255,.22); filter:blur(18px); }
.hero::after { content:""; position:absolute; width:360px; height:360px; right:-150px; bottom:-220px; border-radius:50%; background:rgba(138,77,255,.20); filter:blur(18px); }
.hero > * { position:relative; z-index:1; }
.hero h1 { max-width:800px; margin:18px auto; font-size:clamp(45px,7vw,76px); line-height:1.03; letter-spacing:-2px; }
.hero p { max-width:720px; margin:0 auto 28px; color:#b9c5de; font-size:20px; line-height:1.65; }
.hero-buttons { display:flex; flex-wrap:wrap; justify-content:center; gap:11px; }
.hero-primary { padding:17px 30px; font-weight:800; box-shadow:0 12px 30px rgba(70,91,255,.34); }
.hero-secondary { padding:15px 24px; border:1px solid rgba(105,163,255,.30); background:rgba(105,163,255,.10); box-shadow:none; }
.home-section { max-width:1100px; margin:0 auto; padding:56px 20px 12px; text-align:center; }
.home-section h2 { font-size:35px; margin:8px 0; }
.home-section-intro { max-width:720px; margin:0 auto 30px; color:#aeb9d1; line-height:1.7; }
.step-card { width:320px; min-height:245px; text-align:left; transition:.2s ease; }
.step-card:hover { transform:translateY(-6px); border-color:rgba(105,163,255,.55); }
.step-number { width:44px; height:44px; display:grid; place-items:center; border-radius:14px; background:linear-gradient(135deg,#347cff,#7857ff); font-weight:900; box-shadow:0 9px 22px rgba(67,88,255,.28); }
.step-card h3 { font-size:23px; margin:18px 0 8px; }
.step-card p { color:#aeb9d1; line-height:1.65; }
.quick-card { width:245px; min-height:175px; text-align:center; transition:.2s ease; }
.quick-card:hover { transform:translateY(-5px); border-color:rgba(105,163,255,.55); }
.quick-icon { font-size:31px; margin-bottom:12px; }
.home-cta { max-width:900px; margin:55px auto 80px; padding:45px 30px; text-align:center; border:1px solid rgba(105,163,255,.28); border-radius:25px; background:linear-gradient(135deg,rgba(52,124,255,.16),rgba(120,87,255,.14)); }
.features,.container { display:flex; flex-wrap:wrap; justify-content:center; align-items:flex-start; gap:25px; padding:35px 20px; }
.card { width:400px; padding:30px; background:rgba(20,27,45,.92); border:1px solid #293654; border-radius:16px; box-shadow:0 15px 40px rgba(0,0,0,.3); }
.wide-card { width:min(900px,100%); }
.feature-card { width:260px; text-align:center; }
.main-button,.submit-button,.clear-button { display:inline-block; margin:5px; padding:15px 28px; border:0; border-radius:10px; color:white; cursor:pointer; font-size:17px; text-decoration:none; }
.main-button,.submit-button { background:linear-gradient(135deg,#347cff,#7857ff); }
.clear-button { width:100%; margin-top:22px; background:#b8324a; }
button:hover,.main-button:hover { transform:translateY(-2px); }
label { display:block; margin:17px 0 7px; color:#cbd4e8; }
input,select,textarea { width:100%; padding:12px; background:#0d1322; border:1px solid #33405f; border-radius:8px; color:white; font-size:16px; }
textarea { resize:vertical; }
.submit-button { width:100%; margin-top:24px; }
.profit,.history-profit,.listing-price { color:#5ee59d; font-weight:bold; }
.profit { font-size:24px; }
.listing-price { font-size:20px; }
.warning { color:#ffbe55; }
.tag { color:#69a3ff; font-weight:bold; letter-spacing:2px; }
.history-item { padding:12px 0; border-bottom:1px solid #293654; }
.listing-image { width:100%; height:220px; object-fit:contain; background:white; border-radius:10px; }
.error { color:#ff8b8b; }
.filter-summary { color:#aeb9d1; text-align:center; width:100%; }

.auth-login { border:1px solid #517dff; background:rgba(52,124,255,.12); box-shadow:inset 0 0 18px rgba(52,124,255,.12); }
.auth-login:hover { background:#347cff; box-shadow:0 8px 22px rgba(52,124,255,.35); }
.auth-register { background:linear-gradient(135deg,#347cff,#8a4dff); font-weight:bold; box-shadow:0 8px 22px rgba(103,80,255,.32); }
.auth-register:hover { box-shadow:0 12px 28px rgba(103,80,255,.48); }
.auth-card { position:relative; overflow:hidden; width:430px; border:1px solid rgba(105,163,255,.45); background:linear-gradient(145deg,rgba(24,36,69,.97),rgba(14,19,34,.97)); }
.auth-card::before { content:""; position:absolute; width:180px; height:180px; border-radius:50%; background:rgba(91,90,255,.16); filter:blur(8px); top:-95px; right:-75px; pointer-events:none; }
.auth-icon { width:62px; height:62px; display:grid; place-items:center; margin:0 auto 18px; border-radius:20px; font-size:29px; background:linear-gradient(135deg,#347cff,#7857ff); box-shadow:0 12px 30px rgba(91,90,255,.38); }
.auth-title { text-align:center; margin:8px 0; font-size:34px; }
.auth-subtitle { text-align:center; color:#aeb9d1; margin:0 0 24px; }
.auth-submit { font-weight:bold; box-shadow:0 10px 25px rgba(91,90,255,.30); transition:transform .18s ease,box-shadow .18s ease; }
.auth-submit:hover { transform:translateY(-2px); box-shadow:0 14px 32px rgba(91,90,255,.45); }


.pro-hero{max-width:1050px;margin:38px auto 10px;padding:58px 30px;text-align:center;border:1px solid rgba(105,163,255,.28);border-radius:30px;background:linear-gradient(145deg,rgba(30,50,96,.96),rgba(12,17,34,.98));box-shadow:0 28px 70px rgba(0,0,0,.38)}
.pro-hero h1{margin:12px 0;font-size:clamp(38px,6vw,64px)}.pro-hero p{max-width:700px;margin:0 auto;color:#b9c5de;font-size:18px;line-height:1.65}
.pro-grid{max-width:1100px;margin:0 auto 60px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:22px;padding:28px 20px}
.pro-plan{position:relative;width:auto;min-height:430px;display:flex;flex-direction:column;border-color:rgba(105,163,255,.3);transition:.2s}.pro-plan:hover{transform:translateY(-7px);border-color:#69a3ff}.pro-plan.popular{border:2px solid #7857ff;box-shadow:0 18px 55px rgba(91,90,255,.3)}
.pro-badge{position:absolute;top:-14px;left:50%;transform:translateX(-50%);padding:7px 16px;border-radius:999px;background:linear-gradient(135deg,#347cff,#7857ff);font-size:12px;font-weight:900;letter-spacing:1px;white-space:nowrap}.pro-price{margin:10px 0 4px;font-size:45px;font-weight:900}.pro-period{color:#aeb9d1}.pro-list{flex:1;margin:23px 0;padding:0;list-style:none;text-align:left}.pro-list li{margin:13px 0;color:#cbd4e8}.pro-list li::before{content:"✓";margin-right:10px;color:#5ee59d;font-weight:900}.pro-button{width:100%;margin:0;font-weight:900}
.pro-home-strip{max-width:1050px;margin:45px auto;padding:35px 28px;display:flex;align-items:center;justify-content:space-between;gap:25px;border-radius:24px;border:1px solid rgba(120,87,255,.45);background:linear-gradient(135deg,rgba(52,124,255,.2),rgba(120,87,255,.2))}.pro-home-strip h2{margin:0 0 8px}.pro-home-strip p{margin:0;color:#b9c5de}@media(max-width:850px){.pro-grid{grid-template-columns:1fr}.pro-home-strip{margin:35px 20px;flex-direction:column;text-align:center}}

.preview-modal-backdrop{position:fixed;inset:0;z-index:9999;display:none;align-items:center;justify-content:center;padding:20px;background:rgba(3,7,18,.78);backdrop-filter:blur(10px)}
.preview-modal{width:min(560px,100%);padding:34px;border:1px solid rgba(105,163,255,.5);border-radius:26px;background:linear-gradient(145deg,#192a55,#0d1324);box-shadow:0 30px 90px rgba(0,0,0,.58);text-align:center}.preview-clock{display:inline-block;margin:12px 0 20px;padding:10px 18px;border-radius:999px;background:rgba(94,229,157,.12);color:#5ee59d;font-weight:900;font-size:20px}.preview-actions{display:flex;flex-wrap:wrap;justify-content:center;gap:10px}.preview-pill{position:fixed;right:18px;bottom:18px;z-index:9998;padding:10px 15px;border-radius:999px;background:rgba(8,11,20,.9);font-weight:800}

.community-layout { width:min(1050px,100%); display:grid; grid-template-columns:380px 1fr; gap:24px; align-items:start; }
.community-form-card { position:sticky; top:105px; }
.comment-list { display:flex; flex-direction:column; gap:16px; }
.comment-card { width:100%; padding:22px; }
.comment-header { display:flex; justify-content:space-between; align-items:center; gap:15px; margin-bottom:14px; }
.comment-user { display:flex; align-items:center; gap:12px; }
.comment-avatar { width:44px; height:44px; display:grid; place-items:center; border-radius:50%; color:white; font-weight:900; background:linear-gradient(135deg,var(--accent,#347cff),#7857ff); }
.comment-text { margin:0; color:#d5dced; line-height:1.7; white-space:pre-wrap; overflow-wrap:anywhere; }
.comment-date { color:#8f9bb4; font-size:13px; }
.comment-photo { width:100%; max-height:480px; object-fit:contain; margin:0 0 18px; border-radius:14px; background:#fff; }
.upload-help { color:#8f9bb4; font-size:14px; line-height:1.5; }
.selling-notice { padding:15px; margin:20px 0; border:1px solid rgba(255,190,85,.4); border-radius:12px; background:rgba(255,190,85,.08); color:#ffd68f; line-height:1.5; }
.chart-container { position:relative; width:100%; height:440px; margin-top:30px; padding:22px; border:1px solid #293654; border-radius:16px; background:rgba(9,14,27,.72); }
@media (max-width:850px) { .community-layout { grid-template-columns:1fr; } .community-form-card { position:static; } .comment-header { align-items:flex-start; flex-direction:column; } }


.sell-hero { text-align:center; padding:42px 32px; background:linear-gradient(145deg,rgba(52,124,255,.18),rgba(138,77,255,.16)); }
.sell-hero h1 { margin:8px 0 12px; font-size:clamp(34px,5vw,54px); }
.sell-kicker { color:#5ee59d; font-weight:900; letter-spacing:2px; }
.featured-listing { border-color:#ffbe55!important; box-shadow:0 18px 50px rgba(255,190,85,.13); }
.featured-badge { display:inline-flex; padding:7px 11px; margin-bottom:12px; border-radius:999px; background:rgba(255,190,85,.14); color:#ffcf78; font-weight:900; font-size:13px; letter-spacing:1px; }
.sitewide-sponsored-ad{max-width:1180px;margin:18px auto 4px;padding:14px 18px;border:1px solid rgba(255,190,85,.55);border-radius:16px;background:linear-gradient(100deg,rgba(255,190,85,.13),rgba(52,124,255,.10));display:flex;align-items:center;gap:16px;box-shadow:0 14px 34px rgba(0,0,0,.18)}.sitewide-sponsored-ad img{width:72px;height:58px;object-fit:cover;border-radius:10px}.sitewide-sponsored-copy{min-width:0;flex:1}.sitewide-sponsored-copy strong{display:block;color:#ffcf78;letter-spacing:.8px}.sitewide-sponsored-copy p{margin:3px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.sitewide-sponsored-ad .main-button{white-space:nowrap}@media(max-width:650px){.sitewide-sponsored-ad{margin:12px;align-items:flex-start;flex-wrap:wrap}.sitewide-sponsored-copy p{white-space:normal}.sitewide-sponsored-ad .main-button{width:100%;text-align:center}}
.promo-box { padding:16px; margin:18px 0; border:1px solid rgba(105,163,255,.35); border-radius:13px; background:rgba(105,163,255,.08); }
.vendor-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(270px,1fr)); gap:22px; width:min(1100px,100%); }
.vendor-card { width:100%; }
.vendor-logo { width:76px; height:76px; object-fit:cover; border-radius:20px; background:#fff; }
.vendor-meta { color:#aeb9d1; line-height:1.7; }


.nav-groups { display:flex; align-items:center; gap:10px; flex-wrap:wrap; justify-content:flex-end; }
.nav-group { position:relative; }
.nav-group summary { list-style:none; cursor:pointer; }
.nav-group summary::-webkit-details-marker { display:none; }
.nav-menu { position:absolute; right:0; top:48px; z-index:1000; min-width:230px; padding:12px; border:1px solid rgba(255,255,255,.12); background:#101625; border-radius:16px; box-shadow:0 18px 45px rgba(0,0,0,.35); }
.nav-menu .nav-button { display:block; width:100%; text-align:left; margin:4px 0; }
.store-grid {
  display:grid;
  grid-template-columns:repeat(3,minmax(0,1fr));
  gap:28px;
  width:min(1180px,calc(100% - 40px));
  margin:34px auto;
  align-items:stretch;
}
.store-grid .store-card {
  width:auto;
  min-width:0;
  min-height:270px;
  padding:28px;
  box-sizing:border-box;
  display:flex;
  flex-direction:column;
  justify-content:space-between;
  gap:24px;
  overflow:hidden;
}
.store-card h3 { margin:18px 0 12px; line-height:1.25; }
.store-card p { line-height:1.6; margin:0; overflow-wrap:anywhere; }
.store-card .main-button { width:100%; box-sizing:border-box; text-align:center; margin-top:auto; }
.store-badge { display:inline-block; width:max-content; max-width:100%; padding:6px 10px; border-radius:999px; background:rgba(52,124,255,.15); color:#8fb6ff; font-size:12px; font-weight:800; box-sizing:border-box; }
@media (max-width:1000px) { .store-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } }
@media (max-width:650px) { .store-grid { grid-template-columns:1fr; width:min(100% - 28px,560px); gap:18px; } .store-grid .store-card { min-height:240px; padding:24px; } }


.terms-overlay { position:fixed; inset:0; z-index:10000; display:flex; align-items:center; justify-content:center; padding:20px; background:rgba(3,7,18,.84); backdrop-filter:blur(8px); }
.terms-dialog { width:min(720px,100%); max-height:88vh; overflow:auto; padding:30px; border:1px solid #334264; border-radius:22px; background:#111827; box-shadow:0 28px 80px rgba(0,0,0,.58); }
.terms-dialog h2 { margin:8px 0 14px; font-size:30px; }
.terms-dialog p,.terms-dialog li { color:#c7d0e2; line-height:1.65; }
.terms-summary { margin:18px 0; padding:16px 18px; border:1px solid rgba(105,163,255,.25); border-radius:14px; background:rgba(105,163,255,.08); }
.terms-actions { display:flex; flex-wrap:wrap; gap:12px; margin-top:22px; }
.terms-actions .main-button { width:auto; }
.terms-page { width:min(900px,calc(100% - 32px)); margin:40px auto 70px; }
.terms-page h2 { margin-top:32px; }


.metric-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:18px; width:min(1180px,calc(100% - 40px)); margin:26px auto; }
.metric-card { width:auto; min-width:0; }
.metric-value { font-size:30px; font-weight:900; margin:8px 0; color:#8fb6ff; }
.dashboard-grid { display:grid; grid-template-columns:minmax(300px,.9fr) minmax(420px,1.4fr); gap:24px; width:min(1180px,calc(100% - 40px)); margin:26px auto; align-items:start; }
.dashboard-grid .card { width:auto; box-sizing:border-box; }
.sales-table-wrap { overflow-x:auto; }
.sales-table { width:100%; border-collapse:collapse; min-width:760px; }
.sales-table th,.sales-table td { padding:12px; border-bottom:1px solid #293654; text-align:left; }
.profit-chart { width:100%; height:330px; display:block; border-radius:14px; background:rgba(7,12,25,.46); }
@media (max-width:950px) { .metric-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .dashboard-grid { grid-template-columns:1fr; } }
@media (max-width:600px) { .metric-grid { grid-template-columns:1fr; } }

@media (max-width:750px) { .hero h1 { font-size:42px; } nav { padding:18px; flex-direction:column; gap:12px; } .card { width:100%; } .nav-button { margin:3px; } }

.locale-picker{position:relative;z-index:1100}.locale-picker details{position:relative}.locale-picker summary{list-style:none;cursor:pointer;padding:10px 14px;border:1px solid rgba(105,163,255,.35);border-radius:999px;background:rgba(105,163,255,.10);color:white;font-weight:800}.locale-picker summary::-webkit-details-marker{display:none}.locale-panel{position:absolute;right:0;top:50px;width:min(410px,calc(100vw - 30px));max-height:72vh;overflow:auto;padding:16px;border:1px solid #334264;border-radius:18px;background:#101625;box-shadow:0 24px 60px rgba(0,0,0,.5)}.country-summary{display:flex!important;align-items:center;gap:7px}.country-summary-flag{width:26px;height:19px;object-fit:cover;border-radius:3px;box-shadow:0 0 0 1px rgba(255,255,255,.24)}.country-flag-img{width:34px;height:25px;object-fit:cover;border-radius:4px;box-shadow:0 0 0 1px rgba(255,255,255,.22);flex:0 0 auto}.country-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.country-option{display:flex;align-items:center;gap:9px;width:100%;padding:10px;border:1px solid #293654;border-radius:10px;background:#151e33;color:white;cursor:pointer;text-align:left}.country-option.active{border-color:#69a3ff;background:rgba(52,124,255,.18)}.locale-note{font-size:12px;color:#9facbf;line-height:1.45}.vendor-locked{opacity:.78;border-style:dashed}.currency-code{font-size:.78em;opacity:.72;margin-left:4px}

.onboarding-shell{position:fixed;inset:0;z-index:9998;background:rgba(3,7,14,.86);backdrop-filter:blur(8px);display:grid;place-items:center;padding:22px;overflow:auto}.onboarding-shell>.card{width:min(1050px,96vw);max-height:92vh;overflow:auto;box-shadow:0 30px 90px rgba(0,0,0,.65)}.choice-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}.choice-card{display:block;cursor:pointer;border:1px solid #2b3958;border-radius:18px;padding:14px;background:#141d31}.choice-card input{width:auto;margin-right:8px}.background-preview{height:85px;border-radius:12px;margin-bottom:10px}.bg-midnight{background:radial-gradient(circle at 20% 20%,#24467a,#090d17 70%)}.bg-ocean{background:linear-gradient(135deg,#06283d,#1363df,#47b5ff)}.bg-sunset{background:linear-gradient(135deg,#28104e,#9b1d73,#ff6b6b)}.bg-forest{background:linear-gradient(135deg,#071a12,#0f5132,#3a7d44)}.bg-graphite{background:linear-gradient(135deg,#111,#333,#101827)}.tutorial-step{display:none}.tutorial-step.active{display:block}.onboarding-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:22px}.apple-chart-card{max-width:900px;margin:30px auto}.chart-wrap{height:310px}
</style>
"""

NAV = """
<nav>
  <div class="logo" onclick="window.location.href='/'">FLIPFINDER</div>
  <div class="nav-groups">
    <button class="nav-button auth-login" onclick="window.location.href='/'">Stores Home</button>
    <details class="nav-group nav-exclusive"><summary class="nav-button">Search Stores ▾</summary><div class="nav-menu">
      <button class="nav-button" onclick="window.location.href='/ebay-search'">eBay</button>
      <button class="nav-button" onclick="window.location.href='/facebook-search'">Facebook Marketplace</button>
      <button class="nav-button" onclick="window.location.href='/gumtree-search'">Gumtree</button>
      <button class="nav-button" onclick="window.location.href='/temu-search'">Temu</button>
      <button class="nav-button" onclick="window.location.href='/alibaba-search'">Alibaba</button>
      <button class="nav-button" onclick="window.location.href='/vendor-access'">Vendor Directory</button>{% if vendor_access %}<button class="nav-button" onclick="window.location.href='/vendor-profile'">My Vendor Profile</button>{% endif %}
    </div></details>
    <details class="nav-group nav-exclusive"><summary class="nav-button">FlipFinder Tools ▾</summary><div class="nav-menu">
      <button class="nav-button" onclick="window.location.href='/workspace'">Tools Dashboard</button>
      <button class="nav-button" onclick="window.location.href='/account-home'">Profit Dashboard</button>
      <button class="nav-button" onclick="window.location.href='/analyse'">Analyse Deal</button>
      <button class="nav-button" onclick="window.location.href='/market-values'">Market Values</button>
      <button class="nav-button" onclick="window.location.href='/serial-check'">Apple Serial Check</button>
      <button class="nav-button" onclick="window.location.href='/price-alerts'">Price Alerts</button>
      <button class="nav-button" onclick="window.location.href='/iphone-community'">Sell an iPhone</button>
      <button class="nav-button" onclick="window.location.href='/reports'">Reports</button>
      <button class="nav-button auth-register" onclick="window.location.href='/pricing'">Pro Plans</button>
    </div></details>
    <div class="locale-picker"><details class="nav-exclusive"><summary class="nav-button country-summary"><img class="country-summary-flag" src="https://flagcdn.io/{{ country_code|lower }}.svg" alt="{{ country.name }} flag"> {{ country.name }} ▾</summary><div class="locale-panel">
      <p class="tag" style="margin-top:0">COUNTRY & LANGUAGE</p>
      <form method="post" action="{{ url_for('set_locale') }}">
        <div class="country-grid">{% for code,item in countries.items() %}<button class="country-option {% if code==country_code %}active{% endif %}" type="submit" name="country_code" value="{{ code }}" data-default-location="{{ item.default_location }}" onclick="document.getElementById('navSearchLocation').value=this.dataset.defaultLocation"><img class="country-flag-img" src="https://flagcdn.io/{{ code|lower }}.svg" alt="{{ item.name }} flag"><span><strong>{{ item.name }}</strong><br><small>{{ item.currency }}</small></span></button>{% endfor %}</div>
        <label>Nearby phone search location</label><input id="navSearchLocation" name="search_location" value="{{ search_location }}" placeholder="City, suburb, state or postcode" autocomplete="postal-code">
        <p class="locale-note" style="margin-top:6px">Facebook Marketplace and supported local searches start from this area.</p>
        <label>Website language</label><select name="language">{% for code,name in languages.items() %}<option value="{{ code }}" {% if code==language %}selected{% endif %}>{{ name }}</option>{% endfor %}</select>
        <button class="submit-button" type="submit">Apply selection</button>
      </form>
      <p class="locale-note">Your country controls the displayed currency. Stripe Checkout shows the final payment amount.</p>
    </div></details></div>
    {% if session.get('user_id') %}
      <button class="nav-button auth-register" onclick="window.location.href='/account'">My Account</button>
      <button class="nav-button auth-logout" onclick="window.location.href='/logout'">Log Out</button>
    {% else %}
      <button class="nav-button auth-login" onclick="window.location.href='/login'">Log In</button>
      <button class="nav-button auth-register" onclick="window.location.href='/register'">Create Account</button>
    {% endif %}
  </div>
</nav>
<script>
(function(){
  function setupExclusiveNav(){
    const dropdowns=[...document.querySelectorAll('nav details.nav-exclusive')];
    dropdowns.forEach((dropdown)=>{
      dropdown.addEventListener('toggle',()=>{
        if(!dropdown.open)return;
        dropdowns.forEach((other)=>{if(other!==dropdown)other.open=false;});
      });
    });
    document.addEventListener('pointerdown',(event)=>{
      if(!event.target.closest('nav details.nav-exclusive')){
        dropdowns.forEach((dropdown)=>dropdown.open=false);
      }
    });
    document.addEventListener('keydown',(event)=>{
      if(event.key==='Escape')dropdowns.forEach((dropdown)=>dropdown.open=false);
    });
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',setupExclusiveNav);
  else setupExclusiveNav();
})();
</script>
"""


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_sitewide_sponsored_listing():
    try:
        with sqlite3.connect(DB_PATH) as connection:
            connection.row_factory=sqlite3.Row
            return connection.execute("""
                SELECT iphone_comments.id, iphone_comments.comment, iphone_comments.image_filename, users.username
                FROM iphone_comments JOIN users ON users.id=iphone_comments.user_id
                WHERE iphone_comments.featured=1
                ORDER BY iphone_comments.id DESC LIMIT 1
            """).fetchone()
    except sqlite3.Error:
        return None

def page(title, body):
    user = get_current_user()
    accent = user["accent_color"] if user and user["accent_color"] else "#347cff"
    theme = user["theme"] if user and user["theme"] else "dark"
    compact = bool(user["compact_cards"]) if user else False
    background_style = user["background_style"] if user and "background_style" in user.keys() else "midnight"
    backgrounds={"midnight":"radial-gradient(circle at 20% 10%,#182b4d,#070a12 62%)","ocean":"linear-gradient(135deg,#061a2b,#0a456e,#0b1630)","sunset":"linear-gradient(135deg,#1b102b,#52194d,#151020)","forest":"linear-gradient(135deg,#07150f,#123b2a,#08110d)","graphite":"linear-gradient(135deg,#101114,#252a33,#0a0b0d)"}
    page_background=backgrounds.get(background_style,backgrounds["midnight"])
    surface_color=(user["surface_color"] if user and "surface_color" in user.keys() else "#141d31")
    text_color=(user["text_color"] if user and "text_color" in user.keys() else "#eef4ff")
    muted_color=(user["muted_color"] if user and "muted_color" in user.keys() else "#aebbd0")
    nav_color=(user["nav_color"] if user and "nav_color" in user.keys() else "#0b1220")
    button_text_color=(user["button_text_color"] if user and "button_text_color" in user.keys() else "#ffffff")
    theme_css = f"""
    <style>
      :root {{ --accent:{accent}; --surface:{surface_color}; --text:{text_color}; --muted:{muted_color}; --nav:{nav_color}; --button-text:{button_text_color}; }} body {{ background:{page_background}; background-attachment:fixed; color:var(--text)!important; }} .card,.choice-card,.nav-menu,.locale-panel{{background:var(--surface)!important;color:var(--text)!important}} nav{{background:var(--nav)!important}} p,small,label,.home-section-intro{{color:var(--muted)}} .main-button,.submit-button{{background:var(--accent)!important;color:var(--button-text)!important}} h1,h2,h3,strong{{color:var(--text)}}
      .main-button,.submit-button,.auth-register,.auth-icon {{ background:linear-gradient(135deg,var(--accent),#7857ff)!important; }}
      .logo,.tag {{ color:var(--accent)!important; }}
      {'.card { padding:18px!important; } .listing-image { height:170px!important; }' if compact else ''}
      {"body { background:#f3f6fb!important; color:#111827!important; } nav { background:rgba(255,255,255,.94)!important; } .card,.hero,.home-cta { background:#ffffff!important; color:#111827!important; } p,label,h1,h2,h3,strong,.hero p,.auth-subtitle,.filter-summary,.home-section-intro,.step-card p { color:#111827!important; } .nav-button { color:#111827!important; } input,select,textarea { background:#f7f9fc!important; color:#111827!important; border-color:#cbd5e1!important; }" if theme == 'light' else ''}
    </style>
    """
    country_code=current_country_code(); country=COUNTRIES[country_code]; language=current_language()
    rendered_nav = render_template_string(NAV, vendor_access=_can_use_vendor_tools(user) if user else False, country=country, countries=COUNTRIES, country_code=country_code, languages=LANGUAGES, language=language, search_location=current_search_location())
    terms_version = "2026-10-07"
    show_terms = request.cookies.get("flipfinder_terms") != terms_version and request.endpoint not in {"terms", "accept_terms"}
    terms_modal = """
    {% if show_terms %}
    <div class="terms-overlay" id="termsOverlay" role="dialog" aria-modal="true" aria-labelledby="termsTitle">
      <div class="terms-dialog">
        <p class="tag">BEFORE YOU CONTINUE</p><h2 id="termsTitle">FlipFinder Terms and Conditions</h2>
        <p>Please review and accept the terms before using FlipFinder.</p>
        <div class="terms-summary"><ul>
          <li>FlipFinder provides research, comparison and calculation tools, not financial, legal or purchasing advice.</li>
          <li>Marketplace listings, prices, vendors, coverage information and external links may change or be inaccurate. Verify everything before paying.</li>
          <li>FlipFinder is not affiliated with Apple, eBay, Meta, Gumtree, Temu, Alibaba or listed vendors unless explicitly stated.</li>
          <li>Do not misuse the service, submit unlawful content, scrape the website, or attempt to bypass security or subscription controls.</li>
          <li>Australian Consumer Law rights are not excluded.</li>
        </ul></div>
        <label style="display:flex;gap:10px;align-items:flex-start"><input id="termsConfirm" type="checkbox" style="width:auto;margin-top:5px"> <span>I have read and agree to the Terms and Conditions.</span></label>
        <div class="terms-actions"><button class="main-button" id="acceptTermsButton" type="button" disabled>Accept and Continue</button><a class="main-button hero-secondary" href="/terms">Read Full Terms</a><button class="clear-button" id="declineTermsButton" type="button" style="width:auto">Decline</button></div>
        <p id="termsError" class="error" style="display:none"></p>
      </div>
    </div>
    <script>
    (function(){
      const check=document.getElementById('termsConfirm'), accept=document.getElementById('acceptTermsButton'), decline=document.getElementById('declineTermsButton'), error=document.getElementById('termsError');
      if(!check||!accept)return;
      check.addEventListener('change',()=>accept.disabled=!check.checked);
      accept.addEventListener('click',async()=>{ accept.disabled=true; accept.textContent='Saving...'; try { const response=await fetch('/accept-terms',{method:'POST',headers:{'X-Requested-With':'fetch'}}); if(!response.ok) throw new Error('Could not save acceptance.'); document.getElementById('termsOverlay').remove(); } catch(e) { error.textContent=e.message; error.style.display='block'; accept.disabled=false; accept.textContent='Accept and Continue'; } });
      decline.addEventListener('click',()=>{ document.querySelector('.terms-dialog').innerHTML='<p class="tag">ACCESS DECLINED</p><h2>Terms not accepted</h2><p>FlipFinder cannot be used until the Terms and Conditions are accepted.</p><a class="main-button hero-secondary" href="/terms">Read Full Terms</a>'; });
    })();
    </script>
    {% endif %}
    """
    rendered_terms = render_template_string(terms_modal, show_terms=show_terms)
    sponsored=get_sitewide_sponsored_listing()
    sponsored_ad=""
    if sponsored:
        sponsored_ad=render_template_string("""
        <aside class='sitewide-sponsored-ad' aria-label='Sponsored FlipFinder listing'>
          {% if item.image_filename %}<img src='{{ url_for("iphone_comment_image",filename=item.image_filename) }}' alt='Sponsored product image'>{% endif %}
          <div class='sitewide-sponsored-copy'><strong>SPONSORED PRODUCT · FEATURED AD</strong><p>{{ item.comment }}</p><small>Listed by {{ item.username }}</small></div>
          <a class='main-button' href='{{ url_for("iphone_community") }}#listing-{{ item.id }}'>View Product</a>
        </aside>
        """,item=sponsored)
    locale_widget=render_template_string("""
    <div class='locale-picker'><details><summary>{{ country.flag }} {{ country.currency }} · {{ language_name }}</summary><div class='locale-panel'>
      <h3 style='margin-top:0'>Country, language and currency</h3>
      <form method='post' action='{{ url_for("set_locale") }}'><div class='country-grid'>{% for code,item in countries.items() %}<button class='country-option {% if code==country_code %}active{% endif %}' type='submit' name='country_code' value='{{ code }}'><img class='country-flag-img' src='https://flagcdn.io/{{ code|lower }}.svg' alt='{{ item.name }} flag'><span>{{ item.name }}<br><small>{{ item.currency }}</small></span></button>{% endfor %}</div>
      <label>Website language</label><select name='language'>{% for code,name in languages.items() %}<option value='{{ code }}' {% if code==language %}selected{% endif %}>{{ name }}</option>{% endfor %}</select><button class='submit-button'>Apply country and language</button></form>
      <p class='locale-note'>Displayed A$ amounts are converted using the app's reference rates. Stripe Checkout shows the final billing currency and amount.</p>
    </div></details></div>
    """,country=country,countries=COUNTRIES,country_code=country_code,languages=LANGUAGES,language=language,language_name=LANGUAGES[language],search_location=current_search_location())
    translations={
      "de":{"Stores Home":"Startseite","Search Stores ▾":"Shops durchsuchen ▾","My Account":"Mein Konto","Log In":"Anmelden","Log Out":"Abmelden","Create Account":"Konto erstellen","Browse Stores":"Shops ansehen","Vendor Directory":"Anbieterverzeichnis","Settings":"Einstellungen"},
      "fr":{"Stores Home":"Accueil","Search Stores ▾":"Rechercher des boutiques ▾","My Account":"Mon compte","Log In":"Connexion","Log Out":"Déconnexion","Create Account":"Créer un compte","Browse Stores":"Voir les boutiques","Vendor Directory":"Annuaire des vendeurs","Settings":"Paramètres"},
      "es":{"Stores Home":"Inicio","Search Stores ▾":"Buscar tiendas ▾","My Account":"Mi cuenta","Log In":"Iniciar sesión","Log Out":"Cerrar sesión","Create Account":"Crear cuenta","Browse Stores":"Ver tiendas","Vendor Directory":"Directorio de vendedores","Settings":"Ajustes"},
      "ja":{"Stores Home":"ホーム","Search Stores ▾":"ストアを検索 ▾","My Account":"マイアカウント","Log In":"ログイン","Log Out":"ログアウト","Create Account":"アカウント作成","Browse Stores":"ストアを見る","Vendor Directory":"販売者一覧","Settings":"設定"},
      "hi":{"Stores Home":"होम","Search Stores ▾":"स्टोर खोजें ▾","My Account":"मेरा खाता","Log In":"लॉग इन","Log Out":"लॉग आउट","Create Account":"खाता बनाएँ","Browse Stores":"स्टोर देखें","Vendor Directory":"विक्रेता निर्देशिका","Settings":"सेटिंग्स"},
      "ar":{"Stores Home":"الرئيسية","Search Stores ▾":"البحث في المتاجر ▾","My Account":"حسابي","Log In":"تسجيل الدخول","Log Out":"تسجيل الخروج","Create Account":"إنشاء حساب","Browse Stores":"تصفح المتاجر","Vendor Directory":"دليل البائعين","Settings":"الإعدادات"}
    }
    translation_script=""
    if language!="en":
        mapping=json.dumps(translations.get(language,{}),ensure_ascii=False)
        rtl="document.documentElement.dir='rtl';" if language=="ar" else ""
        translation_script=f"<script>(function(){{const m={mapping};{rtl}document.documentElement.lang={json.dumps(language)};const w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);let n;while(n=w.nextNode()){{const t=n.nodeValue.trim();if(m[t]) n.nodeValue=n.nodeValue.replace(t,m[t]);}}}})();</script>"
    html=render_template_string(
        "<!doctype html><html lang='{{ language }}'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{{ title }}</title>{{ style|safe }}{{ theme_css|safe }}</head><body>{{ nav|safe }}{{ sponsored_ad|safe }}{{ body|safe }}{{ terms|safe }}{{ translation_script|safe }}</body></html>",
        language=language,title=title,style=STYLE,theme_css=theme_css,nav=rendered_nav,sponsored_ad=sponsored_ad,body=body,locale_widget=locale_widget,terms=rendered_terms,translation_script=translation_script
    )
    return localize_aud_text(html)


def setup_database():
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                email TEXT UNIQUE,
                phone TEXT UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                email_verified INTEGER NOT NULL DEFAULT 0,
                phone_verified INTEGER NOT NULL DEFAULT 0,
                two_factor_enabled INTEGER NOT NULL DEFAULT 0,
                two_factor_secret TEXT,
                recovery_codes TEXT,
                accent_color TEXT NOT NULL DEFAULT '#347cff',
                theme TEXT NOT NULL DEFAULT 'dark',
                compact_cards INTEGER NOT NULL DEFAULT 0,
                default_page TEXT NOT NULL DEFAULT '/',
                show_quick_search INTEGER NOT NULL DEFAULT 1,
                show_market_values INTEGER NOT NULL DEFAULT 1
            )
        """)
        expected = {
            "phone": "TEXT", "email_verified": "INTEGER NOT NULL DEFAULT 0",
            "phone_verified": "INTEGER NOT NULL DEFAULT 0", "two_factor_enabled": "INTEGER NOT NULL DEFAULT 0",
            "two_factor_secret": "TEXT", "recovery_codes": "TEXT",
            "accent_color": "TEXT NOT NULL DEFAULT '#347cff'", "theme": "TEXT NOT NULL DEFAULT 'dark'",
            "compact_cards": "INTEGER NOT NULL DEFAULT 0", "default_page": "TEXT NOT NULL DEFAULT '/'",
            "show_quick_search": "INTEGER NOT NULL DEFAULT 1", "show_market_values": "INTEGER NOT NULL DEFAULT 1",
            "stripe_customer_id": "TEXT", "stripe_subscription_id": "TEXT",
            "subscription_status": "TEXT NOT NULL DEFAULT 'free'", "subscription_plan": "TEXT",
            "country_code": "TEXT NOT NULL DEFAULT 'AU'", "language": "TEXT NOT NULL DEFAULT 'en'",
            "currency_code": "TEXT NOT NULL DEFAULT 'AUD'", "search_location": "TEXT",
            "background_style": "TEXT NOT NULL DEFAULT 'midnight'", "show_apple_chart": "INTEGER NOT NULL DEFAULT 1",
            "surface_color": "TEXT NOT NULL DEFAULT '#141d31'", "text_color": "TEXT NOT NULL DEFAULT '#eef4ff'",
            "muted_color": "TEXT NOT NULL DEFAULT '#aebbd0'", "nav_color": "TEXT NOT NULL DEFAULT '#0b1220'",
            "button_text_color": "TEXT NOT NULL DEFAULT '#ffffff'",
            "onboarding_complete": "INTEGER NOT NULL DEFAULT 0", "tutorial_complete": "INTEGER NOT NULL DEFAULT 0"
        }
        user_columns = [row[1] for row in connection.execute("PRAGMA table_info(users)")]
        for name, definition in expected.items():
            if name not in user_columns:
                connection.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL,
                profit REAL NOT NULL, rating TEXT NOT NULL, user_id INTEGER,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        deal_columns = [row[1] for row in connection.execute("PRAGMA table_info(deals)")]
        if "user_id" not in deal_columns:
            connection.execute("ALTER TABLE deals ADD COLUMN user_id INTEGER")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS iphone_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                comment TEXT NOT NULL,
                image_filename TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        comment_columns = [row[1] for row in connection.execute("PRAGMA table_info(iphone_comments)")]
        if "image_filename" not in comment_columns:
            connection.execute("ALTER TABLE iphone_comments ADD COLUMN image_filename TEXT")
        if "featured" not in comment_columns:
            connection.execute("ALTER TABLE iphone_comments ADD COLUMN featured INTEGER NOT NULL DEFAULT 0")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS vendors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                business_name TEXT NOT NULL,
                description TEXT NOT NULL,
                location TEXT NOT NULL,
                website TEXT,
                contact TEXT NOT NULL,
                logo_filename TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                target_type TEXT NOT NULL,
                target_name TEXT NOT NULL,
                target_url TEXT,
                reason TEXT NOT NULL,
                details TEXT,
                status TEXT NOT NULL DEFAULT 'Open',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS price_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                model TEXT NOT NULL,
                max_price REAL NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_checked_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                url TEXT,
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS inventory_sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                quantity_in_stock INTEGER NOT NULL DEFAULT 0,
                quantity_sold INTEGER NOT NULL DEFAULT 0,
                unit_cost REAL NOT NULL DEFAULT 0,
                unit_sale_price REAL NOT NULL DEFAULT 0,
                period TEXT NOT NULL DEFAULT 'week',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS email_verification_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at INTEGER NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS listing_promotions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                stripe_session_id TEXT UNIQUE,
                amount_aud REAL NOT NULL DEFAULT 1.99,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                paid_at TEXT,
                FOREIGN KEY(listing_id) REFERENCES iphone_comments(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)


def setup_password_reset_table():
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at INTEGER NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)


def send_password_reset_email(recipient, reset_url):
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "465"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = (
        os.getenv("SMTP_PASSWORD") or ""
    ).replace(" ", "")
    smtp_from = os.getenv("SMTP_FROM") or smtp_user

    if not smtp_user or not smtp_password or not smtp_from:
        raise RuntimeError("Missing Gmail SMTP settings.")

    message = EmailMessage()
    message["Subject"] = "Reset your FlipFinder password"
    message["From"] = f"FlipFinder <{smtp_from}>"
    message["To"] = recipient

    message.set_content(
        "A password reset was requested for your "
        "FlipFinder account.\n\n"
        f"Reset your password using this link:\n{reset_url}\n\n"
        "This link expires in one hour and can only be used once.\n\n"
        "If you did not request this, you can ignore this email."
    )

    with smtplib.SMTP_SSL(
        smtp_host,
        smtp_port,
        timeout=30
    ) as server:
        server.login(smtp_user, smtp_password)
        server.send_message(message)


def send_verification_email(recipient, verification_url):
    smtp_host=os.getenv("SMTP_HOST","smtp.gmail.com")
    smtp_port=int(os.getenv("SMTP_PORT","465"))
    smtp_user=os.getenv("SMTP_USER")
    smtp_password=(os.getenv("SMTP_PASSWORD") or "").replace(" ","")
    smtp_from=os.getenv("SMTP_FROM") or smtp_user
    if not smtp_user or not smtp_password or not smtp_from:
        raise RuntimeError("Missing Gmail SMTP settings.")
    message=EmailMessage()
    message["Subject"]="Verify your FlipFinder email"
    message["From"]=f"FlipFinder <{smtp_from}>"
    message["To"]=recipient
    message.set_content("Verify that this email belongs to your FlipFinder account.\n\n" f"Verify email: {verification_url}\n\n" "This link expires in one hour and can only be used once. If you did not create this account, ignore this message.")
    with smtplib.SMTP_SSL(smtp_host,smtp_port,timeout=30) as server:
        server.login(smtp_user,smtp_password); server.send_message(message)

def send_security_notice(recipient, subject, text):
    smtp_host=os.getenv("SMTP_HOST","smtp.gmail.com"); smtp_port=int(os.getenv("SMTP_PORT","465")); smtp_user=os.getenv("SMTP_USER"); smtp_password=(os.getenv("SMTP_PASSWORD") or "").replace(" ",""); smtp_from=os.getenv("SMTP_FROM") or smtp_user
    if not smtp_user or not smtp_password or not smtp_from: raise RuntimeError("Missing Gmail SMTP settings.")
    message=EmailMessage(); message["Subject"]=subject; message["From"]=f"FlipFinder <{smtp_from}>"; message["To"]=recipient; message.set_content(text)
    with smtplib.SMTP_SSL(smtp_host,smtp_port,timeout=30) as server: server.login(smtp_user,smtp_password); server.send_message(message)

def issue_email_verification(user_id, email):
    raw_token=secrets.token_urlsafe(32)
    token_hash=hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at=int(time.time())+3600
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("UPDATE email_verification_tokens SET used_at=CURRENT_TIMESTAMP WHERE user_id=? AND used_at IS NULL",(user_id,))
        connection.execute("INSERT INTO email_verification_tokens(user_id,token_hash,expires_at) VALUES(?,?,?)",(user_id,token_hash,expires_at))
    send_verification_email(email,url_for("verify_email",token=raw_token,_external=True))

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def save_deal(phone, profit, rating):
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            "INSERT INTO deals (phone, profit, rating, user_id) VALUES (?, ?, ?, ?)",
            (phone, profit, rating, session.get("user_id"))
        )


def load_deals():
    with sqlite3.connect(DB_PATH) as connection:
        return connection.execute(
            "SELECT phone, profit, rating FROM deals WHERE user_id = ? ORDER BY id DESC LIMIT 5",
            (session.get("user_id"),)
        ).fetchall()


def load_statistics():
    with sqlite3.connect(DB_PATH) as connection:
        return connection.execute("""
            SELECT COUNT(*), COALESCE(AVG(profit),0), COALESCE(MAX(profit),0)
            FROM deals WHERE user_id = ?
        """, (session.get("user_id"),)).fetchone()

def get_ebay_token():
    client_id = os.getenv("EBAY_CLIENT_ID")
    client_secret = os.getenv("EBAY_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("Missing EBAY_CLIENT_ID or EBAY_CLIENT_SECRET in .env")

    response = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        auth=(client_id, client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=15
    )
    response.raise_for_status()
    return response.json()["access_token"]


def search_ebay(model, year="", max_price="", condition="", device_type="iphone"):
    device = DEVICE_CONFIG.get(device_type, DEVICE_CONFIG["iphone"])
    query = " ".join(part for part in [model.strip(), year.strip(), device["suffix"]] if part)
    filters = []
    if max_price:
        filters.extend([f"price:[..{max_price}]", "priceCurrency:AUD"])
    if condition in {"NEW", "USED"}:
        filters.append(f"conditions:{{{condition}}}")

    params = {"q": query, "category_ids": device["ebay_category"], "limit": 100, "sort": "price"}
    if filters:
        params["filter"] = ",".join(filters)

    response = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={"Authorization": f"Bearer {get_ebay_token()}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_AU"},
        params=params,
        timeout=15
    )
    response.raise_for_status()
    items = response.json().get("itemSummaries", [])

    blocked = [
        "case", "cover", "protector", "tempered glass", "charger", "cable",
        "replacement screen", "lcd screen", "digitizer", "repair kit",
        "phone holder", "phone mount", "back glass", "battery replacement",
        "dummy phone", "for parts", "parts only", "box only", "empty box"
    ]
    return [item for item in items if not any(word in item.get("title", "").lower() for word in blocked)][:20]


def _parse_facebook_price(item):
    raw = item.get("priceAmount")
    if raw is None:
        raw = item.get("price")
    if isinstance(raw, dict):
        raw = raw.get("amount") or raw.get("value")
    try:
        return float(raw)
    except (TypeError, ValueError):
        text = str(item.get("priceText", ""))
        cleaned = "".join(ch for ch in text if ch.isdigit() or ch == ".")
        try:
            return float(cleaned)
        except ValueError:
            return 0


def _facebook_image(item):
    image = item.get("photoUrl") or item.get("imageUrl") or item.get("primaryPhoto") or item.get("image") or ""
    if isinstance(image, dict):
        image = image.get("url") or image.get("uri") or ""
    if not image:
        photos = item.get("photos") or item.get("images") or []
        if photos:
            first = photos[0]
            image = first.get("url", "") if isinstance(first, dict) else str(first)
    return image


def search_facebook_marketplace(model, location, max_price="", condition="", device_type="iphone"):
    api_token = os.getenv("APIFY_API_TOKEN")
    if not api_token:
        raise RuntimeError("Missing APIFY_API_TOKEN in .env")

    device = DEVICE_CONFIG.get(device_type, DEVICE_CONFIG["iphone"])
    query = " ".join(part for part in [device["label"], model.strip()] if part)

    run_input = {
        "query": query,
        "startUrls": [],
        "location": location.strip(),
        "radiusKm": 65,
        "strictRadius": False,
        "sortBy": "newest",
        "deliveryMethod": "any",
        "maxResults": 20,
        "excludeEmptyFields": False,
        "descriptionFormat": "all"
    }
    if max_price:
        run_input["maxPrice"] = float(max_price)

    response = requests.post(
        "https://api.apify.com/v2/acts/blackfalcondata~facebook-marketplace-scraper/run-sync-get-dataset-items",
        headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
        json=run_input,
        params={"format": "json", "clean": "true", "maxItems": 20, "maxTotalChargeUsd": 0.10},
        timeout=300
    )
    response.raise_for_status()

    blocked = [
        "wanted", "buying", "found", "box only", "back glass", "replacement",
        "repair", "case", "cover", "protector", "charger", "cable",
        "screen only", "parts only"
    ]
    results = []
    for item in response.json():
        title = str(item.get("title", "")).strip()
        if not title or any(word in title.lower() for word in blocked):
            continue

        price = _parse_facebook_price(item)
        if price <= 0:
            continue
        if max_price and price > float(max_price):
            continue

        item_condition = str(item.get("condition", "Not specified"))
        if condition == "NEW" and "new" not in item_condition.lower():
            continue
        if condition == "USED" and "used" not in item_condition.lower():
            continue

        results.append({
            "title": title,
            "price": price,
            "condition": item_condition,
            "location": item.get("locationText") or item.get("location") or "Not specified",
            "published": item.get("publishedAt", ""),
            "url": item.get("url", "#"),
            "image": _facebook_image(item)
        })
    return results[:20]


def generate_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret, timestamp=None):
    timestamp = int(timestamp or time.time())
    padded = secret + "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(padded, casefold=True)
    counter = struct.pack(">Q", timestamp // 30)
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 15
    number = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff) % 1000000
    return f"{number:06d}"


def verify_totp(secret, code):
    code = code.strip()
    return any(hmac.compare_digest(totp_code(secret, time.time() + offset), code) for offset in (-30, 0, 30))


def hash_recovery(code):
    return hashlib.sha256(code.encode()).hexdigest()


PREVIEW_SECONDS = 900
PAYWALL_ALLOWED_ENDPOINTS = {"home","pricing","create_checkout_session","subscription_success","stripe_webhook","login","register","logout","forgot_password","reset_password","set_locale","accept_terms","terms","vendor_access","onboarding","tutorial","apple_chart_data","verify_email_notice","verify_email","resend_verification","static"}

def _subscription_is_active(user):
    return bool(user and user["subscription_status"] in {"active", "trialing"})

def _can_use_vendor_tools(user):
    return bool(_subscription_is_active(user) and user["subscription_plan"] in {"monthly", "yearly"})


@app.before_request
def enforce_preview_paywall():
    if "preview_started_at_v3" not in session:
        session["preview_started_at_v3"] = int(time.time())
    if request.endpoint is None or request.endpoint in PAYWALL_ALLOWED_ENDPOINTS:
        return None
    user = get_current_user()
    if _subscription_is_active(user):
        return None
    elapsed = int(time.time()) - int(session.get("preview_started_at_v3", int(time.time())))
    if elapsed >= PREVIEW_SECONDS:
        return redirect(url_for("pricing") if session.get("user_id") else url_for("register"))
    return None

def safe_next_url(value, fallback="/"):
    value=(value or "").strip()
    return value if value.startswith("/") and not value.startswith("//") else fallback

@app.route("/register", methods=["GET", "POST"])
def register():
    error_message = ""
    next_url=safe_next_url(request.values.get("next"), "/")
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower() or None
        phone = request.form.get("phone", "").strip() or None
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if len(username) < 3:
            error_message = "Username must be at least 3 characters."
        elif not email:
            error_message = "Enter an email address so FlipFinder can verify the account."
        elif "@" not in email:
            error_message = "Enter a valid email address."
        elif len(password) < 8:
            error_message = "Password must be at least 8 characters."
        elif password != confirm:
            error_message = "Passwords do not match."
        else:
            try:
                database_email = email or f"phone_{hashlib.sha256(phone.encode()).hexdigest()[:12]}@local.invalid"
                with sqlite3.connect(DB_PATH) as connection:
                    cursor = connection.execute(
                        "INSERT INTO users (username,email,phone,password_hash) VALUES (?,?,?,?)",
                        (username,database_email,phone,generate_password_hash(password)))
                    preview_started_at = session.get("preview_started_at_v3", int(time.time()))
                    session.clear(); session["preview_started_at_v3"] = preview_started_at; session["user_id"] = cursor.lastrowid; session["username"] = username
                session["after_tutorial"] = next_url
                try:
                    issue_email_verification(cursor.lastrowid,email)
                    session["verification_sent"] = True
                except (RuntimeError,OSError,smtplib.SMTPException) as exc:
                    session["verification_error"] = str(exc)
                return redirect(url_for("verify_email_notice"))
            except sqlite3.IntegrityError:
                error_message = "That username, email or phone number is already registered."
    template = """
    <div class="container"><div class="card auth-card"><div class="auth-icon">✦</div>
    <p class="tag" style="text-align:center">CREATE ACCOUNT</p><h1 class="auth-title">Join FlipFinder</h1>
    <p class="auth-subtitle">Use an email address, phone number, or both.</p>
    {% if error %}<p class="error">{{ error }}</p>{% endif %}<form method="post"><input type="hidden" name="next" value="{{ next_url }}">
    <label>Username</label><input name="username" required minlength="3">
    <label>Email (optional)</label><input name="email" type="email">
    <label>Phone number (optional)</label><input name="phone" type="tel" placeholder="Example: +61 4xx xxx xxx">
    <label>Password</label><input name="password" type="password" required minlength="8">
    <label>Confirm password</label><input name="confirm_password" type="password" required minlength="8">
    <button class="submit-button auth-submit">Create My Account</button></form></div></div>"""
    return page("Create Account | FlipFinder", render_template_string(template,error=error_message))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    message = ""
    error = ""
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip().lower()
        # Use the same public response whether or not an account exists.
        message = "If a matching account exists, a password-reset link has been sent."
        with sqlite3.connect(DB_PATH) as connection:
            connection.row_factory = sqlite3.Row
            user = connection.execute(
                "SELECT * FROM users WHERE lower(email)=? OR phone=?",
                (identifier, identifier)
            ).fetchone()

        if user and user["email"] and not user["email"].endswith("@local.invalid"):
            raw_token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            expires_at = int(time.time()) + 3600
            with sqlite3.connect(DB_PATH) as connection:
                connection.execute(
                    "UPDATE password_reset_tokens SET used_at=CURRENT_TIMESTAMP WHERE user_id=? AND used_at IS NULL",
                    (user["id"],)
                )
                connection.execute(
                    "INSERT INTO password_reset_tokens(user_id,token_hash,expires_at) VALUES(?,?,?)",
                    (user["id"], token_hash, expires_at)
                )
            reset_url = url_for("reset_password", token=raw_token, _external=True)
            try:
                send_password_reset_email(user["email"], reset_url)
            except (RuntimeError, OSError, smtplib.SMTPException) as exc:
                print("EMAIL ERROR:", type(exc).__name__, str(exc))
                if app.debug:
                    error = f"Email error: {type(exc).__name__}: {exc}"

    body = render_template_string("""
    <div class="container"><div class="card auth-card">
      <div class="auth-icon">?</div><p class="tag" style="text-align:center">ACCOUNT RECOVERY</p>
      <h1 class="auth-title">Forgot password?</h1>
      <p class="auth-subtitle">Enter the email address or phone number connected to the account.</p>
      {% if message %}<p style="color:#5ee59d">{{ message }}</p>{% endif %}
      {% if error %}<p class="error">{{ error }}</p>{% endif %}
      <form method="post"><label>Email or phone number</label><input name="identifier" required autocomplete="username"><button class="submit-button">Send Reset Link</button></form>
      <p style="text-align:center"><a href="/login" style="color:#69a3ff">Back to Log In</a></p>
    </div></div>
    """, message=message, error=error)
    return page("Forgot Password | FlipFinder", body)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        reset = connection.execute(
            """SELECT * FROM password_reset_tokens
            WHERE token_hash=? AND used_at IS NULL AND expires_at>?""",
            (token_hash, int(time.time()))
        ).fetchone()

    if not reset:
        return page("Reset Link Invalid | FlipFinder", "<div class='container'><div class='card'><h1>Reset link expired or invalid</h1><p>Request a new password-reset link.</p><a class='main-button' href='/forgot-password'>Request New Link</a></div></div>")

    error = ""
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            with sqlite3.connect(DB_PATH) as connection:
                connection.execute(
                    "UPDATE users SET password_hash=? WHERE id=?",
                    (generate_password_hash(password), reset["user_id"])
                )
                connection.execute(
                    "UPDATE password_reset_tokens SET used_at=CURRENT_TIMESTAMP WHERE id=?",
                    (reset["id"],)
                )
                connection.execute(
                    "UPDATE password_reset_tokens SET used_at=CURRENT_TIMESTAMP WHERE user_id=? AND used_at IS NULL",
                    (reset["user_id"],)
                )
            session.clear()
            return page("Password Updated | FlipFinder", "<div class='container'><div class='card'><h1>Password updated</h1><p>You can now log in with the new password.</p><a class='main-button' href='/login'>Log In</a></div></div>")

    body = render_template_string("""
    <div class="container"><div class="card auth-card">
      <p class="tag" style="text-align:center">SECURE RESET</p><h1 class="auth-title">Choose a new password</h1>
      {% if error %}<p class="error">{{ error }}</p>{% endif %}
      <form method="post"><label>New password</label><input name="password" type="password" minlength="8" required autocomplete="new-password"><label>Confirm new password</label><input name="confirm_password" type="password" minlength="8" required autocomplete="new-password"><button class="submit-button">Update Password</button></form>
    </div></div>
    """, error=error)
    return page("Reset Password | FlipFinder", body)


@app.route("/verify-email", methods=["GET"])
@login_required
def verify_email_notice():
    user=get_current_user()
    if user["email_verified"]: return redirect(url_for("onboarding"))
    body=render_template_string("""<div class='onboarding-shell'><div class='card wide-card'><p class='tag'>VERIFY EMAIL</p><h1>Check your inbox</h1><p>A verification link was sent to <strong>{{ email }}</strong>. Open the link to confirm that the email belongs to this account.</p>{% if error %}<p class='error'>{{ error }}</p>{% endif %}<form method='post' action='{{ url_for("resend_verification") }}'><button class='submit-button'>Send another verification email</button></form><p class='locale-note'>The verification link expires after one hour and can only be used once.</p></div></div>""",email=user["email"],error=session.pop("verification_error",None))
    return page("Verify Email | FlipFinder",body)

@app.route("/verify-email/<token>")
def verify_email(token):
    token_hash=hashlib.sha256(token.encode()).hexdigest()
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory=sqlite3.Row
        record=connection.execute("SELECT * FROM email_verification_tokens WHERE token_hash=? AND used_at IS NULL",(token_hash,)).fetchone()
        if not record or int(record["expires_at"])<int(time.time()):
            return page("Verification link invalid","<div class='onboarding-shell'><div class='card wide-card'><h1>Verification link expired or invalid</h1><p>Return to FlipFinder and request another verification email.</p></div></div>"),400
        connection.execute("UPDATE users SET email_verified=1 WHERE id=?",(record["user_id"],))
        connection.execute("UPDATE email_verification_tokens SET used_at=CURRENT_TIMESTAMP WHERE id=?",(record["id"],))
    if session.get("user_id")==record["user_id"]: return redirect(url_for("onboarding"))
    return redirect(url_for("login",next=url_for("onboarding")))

@app.route("/resend-verification",methods=["POST"])
@login_required
def resend_verification():
    user=get_current_user()
    if user["email_verified"]: return redirect(url_for("onboarding"))
    try: issue_email_verification(user["id"],user["email"])
    except (RuntimeError,OSError,smtplib.SMTPException) as exc: session["verification_error"]=str(exc)
    return redirect(url_for("verify_email_notice"))

@app.route("/onboarding", methods=["GET", "POST"])
@login_required
def onboarding():
    user=get_current_user()
    if not user["email_verified"]: return redirect(url_for("verify_email_notice"))
    if request.method=="POST":
        if request.form.get("action")!="skip":
            accent=request.form.get("accent_color","#347cff")
            if not re.fullmatch(r"#[0-9a-fA-F]{6}",accent): accent="#347cff"
            background=request.form.get("background_style","midnight")
            if background not in {"midnight","ocean","sunset","forest","graphite"}: background="midnight"
            show_chart=1 if request.form.get("show_apple_chart") else 0
            def colour(name,default):
                value=request.form.get(name,default)
                return value if re.fullmatch(r"#[0-9a-fA-F]{6}",value or "") else default
            surface=colour("surface_color","#141d31"); text=colour("text_color","#eef4ff"); muted=colour("muted_color","#aebbd0"); nav=colour("nav_color","#0b1220"); button_text=colour("button_text_color","#ffffff")
            with sqlite3.connect(DB_PATH) as connection:
                connection.execute("UPDATE users SET accent_color=?,background_style=?,show_apple_chart=?,surface_color=?,text_color=?,muted_color=?,nav_color=?,button_text_color=?,onboarding_complete=1 WHERE id=?",(accent,background,show_chart,surface,text,muted,nav,button_text,session["user_id"]))
        else:
            with sqlite3.connect(DB_PATH) as connection: connection.execute("UPDATE users SET onboarding_complete=1 WHERE id=?",(session["user_id"],))
        return redirect(url_for("tutorial"))
    body=render_template_string("""
    <div class='onboarding-shell'><div class='card wide-card'><p class='tag'>MAKE FLIPFINDER YOURS</p><h1>Choose your home experience</h1><p>These choices can be changed later in Settings.</p>
    <form method='post' id='appearanceForm'><h2>Website colours</h2><div class='choice-grid'><label>Accent and buttons<input name='accent_color' type='color' value='{{ user.accent_color }}'></label><label>Cards and menus<input name='surface_color' type='color' value='{{ user.surface_color }}'></label><label>Main text<input name='text_color' type='color' value='{{ user.text_color }}'></label><label>Secondary text<input name='muted_color' type='color' value='{{ user.muted_color }}'></label><label>Navigation bar<input name='nav_color' type='color' value='{{ user.nav_color }}'></label><label>Button text<input name='button_text_color' type='color' value='{{ user.button_text_color }}'></label></div>
    <h2>Background</h2><div class='choice-grid'>{% for key,label in backgrounds %}<label class='choice-card'><div class='background-preview bg-{{ key }}'></div><input type='radio' name='background_style' value='{{ key }}' {% if user.background_style==key %}checked{% endif %}>{{ label }}</label>{% endfor %}</div>
    <label class='choice-card' style='margin-top:16px'><input type='checkbox' name='show_apple_chart' {% if user.show_apple_chart %}checked{% endif %}> Show the Apple product-value chart on my homepage</label>
    <div class='onboarding-actions'><button class='submit-button' type='submit'>Save and start tutorial</button><button class='main-button hero-secondary' name='action' value='skip' type='submit'>Skip customising</button></div></form></div></div>
    """,user=user,backgrounds=[("midnight","Midnight"),("ocean","Ocean Blue"),("sunset","Sunset"),("forest","Forest"),("graphite","Graphite")])
    return page("Personalise FlipFinder",body)

@app.route("/tutorial", methods=["GET", "POST"])
@login_required
def tutorial():
    if not get_current_user()["email_verified"]: return redirect(url_for("verify_email_notice"))
    if request.method=="POST":
        with sqlite3.connect(DB_PATH) as connection: connection.execute("UPDATE users SET tutorial_complete=1 WHERE id=?",(session["user_id"],))
        return redirect(safe_next_url(session.pop("after_tutorial",None),"/"))
    body="""<div class='onboarding-shell'><div class='card wide-card'><p class='tag'>INTERACTIVE WEBSITE TOUR</p><h1 id='tourTitle'>Open Search Stores</h1><p id='tourText'>Click the highlighted Search Stores button to see where marketplace searches live.</p><div class='card' id='tourStage' style='min-height:240px'><div class='onboarding-actions'><button class='main-button tour-action' data-step='0'>Search Stores ▾</button><button class='nav-button' disabled>FlipFinder Tools ▾</button></div><div id='tourDemo' style='margin-top:18px'></div></div><div class='onboarding-actions'><button class='main-button' id='tourNext' disabled>Complete this action first</button><form method='post'><button class='main-button hero-secondary'>Skip tutorial</button></form></div></div></div><script>
(function(){const lessons=[
 {title:'Open Search Stores',text:'Click Search Stores to view marketplace choices.',button:'Search Stores ▾',demo:'<div class="nav-menu" style="position:static;display:grid"><button class="nav-button tour-choice">eBay</button><button class="nav-button">Facebook Marketplace</button><button class="nav-button">Gumtree</button></div>',needChoice:true},
 {title:'Try a marketplace',text:'Click eBay to preview a store search before continuing.',button:'Search Stores ▾',demo:'<div class="card"><strong>eBay search preview</strong><p>Choose a device, model, condition and maximum price, then run the search.</p></div>'},
 {title:'Open FlipFinder Tools',text:'Click the tools button to view analysis features.',button:'FlipFinder Tools ▾',demo:'<div class="nav-menu" style="position:static;display:grid"><button class="nav-button tour-choice">Analyse Deal</button><button class="nav-button">Market Values</button><button class="nav-button">Price Alerts</button></div>',needChoice:true},
 {title:'Try Analyse Deal',text:'Click Analyse Deal and view how the profit checker works.',button:'FlipFinder Tools ▾',demo:'<div class="card"><strong>Analyse Deal preview</strong><p>Enter buying price, expected selling price and costs to estimate potential profit.</p></div>'},
 {title:'View selling and advertising',text:'Click Sell an iPhone to preview listing and sitewide promotion tools.',button:'Sell an iPhone',demo:'<div class="card"><strong>Sell an iPhone preview</strong><p>Publish a listing, then use Advertise sitewide for A$1.99 to open Stripe Checkout.</p></div>'},
 {title:'Visit Settings',text:'Click Settings to preview colour, chart, country and security controls.',button:'Settings',demo:'<div class="card"><strong>Settings preview</strong><p>Change all website colours, background, Apple chart, location, password, email and replay this tour.</p></div>'}
];let i=0,acted=false;const title=document.getElementById('tourTitle'),text=document.getElementById('tourText'),stage=document.getElementById('tourStage'),demo=document.getElementById('tourDemo'),next=document.getElementById('tourNext');function render(){acted=false;title.textContent=lessons[i].title;text.textContent=lessons[i].text;stage.querySelector('.onboarding-actions').innerHTML='<button class="main-button tour-action">'+lessons[i].button+'</button>';demo.innerHTML='';next.disabled=true;next.textContent='Complete this action first';stage.querySelector('.tour-action').onclick=()=>{demo.innerHTML=lessons[i].demo;if(lessons[i].needChoice){const choice=demo.querySelector('.tour-choice');if(choice)choice.onclick=complete;}else complete();};}function complete(){acted=true;next.disabled=false;next.textContent=i===lessons.length-1?'Finish interactive tour':'Next step';}next.onclick=()=>{if(!acted)return;if(i===lessons.length-1){const f=document.createElement('form');f.method='post';document.body.appendChild(f);f.submit();return;}i++;render();};render();})();
</script>"""
    return page("Interactive FlipFinder Tutorial",body)

@app.route("/apple-chart-data")
def apple_chart_data():
    labels=[]; values=[]
    for name,value in PHONE_VALUES.items():
        if "128GB" in name:
            labels.append(name.replace(" 128GB","")); values.append(value)
    return {"labels":labels,"values":values,"updated":datetime.now().isoformat(timespec="seconds")}

@app.route("/login", methods=["GET", "POST"])
def login():
    error_message = ""
    next_url=safe_next_url(request.values.get("next"), "")
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip().lower()
        password = request.form.get("password", "")
        with sqlite3.connect(DB_PATH) as connection:
            connection.row_factory = sqlite3.Row
            user = connection.execute("SELECT * FROM users WHERE lower(email)=? OR phone=?", (identifier,identifier)).fetchone()
        if user and check_password_hash(user["password_hash"],password):
            if user["two_factor_enabled"]:
                session.clear(); session["pending_2fa_user_id"] = user["id"]
                return redirect("/two-factor-check")
            preview_started_at = session.get("preview_started_at_v3", int(time.time()))
            session.clear(); session["preview_started_at_v3"] = preview_started_at; session["user_id"] = user["id"]; session["username"] = user["username"]
            return redirect(next_url or user["default_page"] or "/account")
        error_message = "Incorrect email, phone number or password."
    template="""<div class="container"><div class="card auth-card"><div class="auth-icon">↗</div>
    <p class="tag" style="text-align:center">WELCOME BACK</p><h1 class="auth-title">Log In</h1>
    {% if error %}<p class="error">{{ error }}</p>{% endif %}<form method="post">
    <label>Email or phone number</label><input name="identifier" required>
    <label>Password</label><input name="password" type="password" required>
    <button class="submit-button auth-submit">Continue to FlipFinder</button></form>
    <p style="text-align:center;margin-top:18px"><a href="/forgot-password" style="color:#69a3ff">Forgot password?</a></p>
    </div></div>"""
    return page("Log In | FlipFinder",render_template_string(template,error=error_message))


@app.route("/two-factor-check", methods=["GET", "POST"])
def two_factor_check():
    pending = session.get("pending_2fa_user_id")
    if not pending: return redirect("/login")
    error = ""
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        user = connection.execute("SELECT * FROM users WHERE id=?",(pending,)).fetchone()
    if request.method == "POST":
        code = request.form.get("code","").strip().replace(" ","")
        recovery = json.loads(user["recovery_codes"] or "[]")
        recovery_hash = hash_recovery(code.upper())
        valid_totp = verify_totp(user["two_factor_secret"],code) if code.isdigit() else False
        valid_recovery = recovery_hash in recovery
        if valid_totp or valid_recovery:
            if valid_recovery:
                recovery.remove(recovery_hash)
                with sqlite3.connect(DB_PATH) as connection:
                    connection.execute("UPDATE users SET recovery_codes=? WHERE id=?",(json.dumps(recovery),user["id"]))
            session.clear(); session["user_id"] = user["id"]; session["username"] = user["username"]
            return redirect(user["default_page"] or "/account")
        error="Invalid verification code."
    body=render_template_string("""<div class="container"><div class="card auth-card"><h1>Two-step verification</h1>
    <p>Enter the six-digit authenticator code or a recovery code.</p>{% if error %}<p class="error">{{ error }}</p>{% endif %}
    <form method="post"><label>Verification code</label><input name="code" required autocomplete="one-time-code"><button class="submit-button">Verify</button></form></div></div>""",error=error)
    return page("Verification | FlipFinder",body)



def stripe_ready():
    return bool(stripe.api_key and all(STRIPE_PRICES.values()))


def stripe_to_dict(value):
    """Convert stripe-python resource objects to ordinary dictionaries."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict_recursive"):
        return value.to_dict_recursive()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return {}


def resolve_stripe_price(plan):
    """Accept either a Stripe price_ ID or product prod_ ID and return a recurring price_ ID."""
    configured_id = (STRIPE_PRICES.get(plan) or "").strip()
    if not configured_id:
        raise RuntimeError(f"Missing STRIPE_PRICE_{plan.upper()} in .env")

    expected_interval = {"weekly": "week", "monthly": "month", "yearly": "year"}.get(plan)
    if not expected_interval:
        raise RuntimeError("Unknown subscription plan.")

    if configured_id.startswith("price_"):
        price = stripe_to_dict(stripe.Price.retrieve(configured_id))
        recurring = stripe_to_dict(price.get("recurring"))
        if not price.get("active"):
            raise RuntimeError(f"The Stripe price for {plan} is inactive.")
        if recurring.get("interval") != expected_interval:
            raise RuntimeError(
                f"The {plan} Stripe price must recur every {expected_interval}, "
                f"but this price uses {recurring.get('interval') or 'no recurring interval'}."
            )
        return price.get("id")

    if configured_id.startswith("prod_"):
        prices = stripe.Price.list(product=configured_id, active=True, type="recurring", limit=100)
        for raw_price in prices.auto_paging_iter():
            price = stripe_to_dict(raw_price)
            recurring = stripe_to_dict(price.get("recurring"))
            if recurring.get("interval") == expected_interval:
                return price.get("id")
        raise RuntimeError(
            f"The Stripe product {configured_id} has no active {expected_interval}ly recurring price. "
            "Create that recurring price in Stripe or put its price_ ID in .env."
        )

    raise RuntimeError(
        f"STRIPE_PRICE_{plan.upper()} must start with price_ or prod_, not {configured_id!r}."
    )


def sync_subscription(subscription, fallback_user_id=None, fallback_plan=None):
    subscription = stripe_to_dict(subscription)
    metadata = stripe_to_dict(subscription.get("metadata"))
    user_id = metadata.get("flipfinder_user_id") or fallback_user_id
    plan = metadata.get("plan") or fallback_plan
    customer_id = subscription.get("customer")
    if not user_id and customer_id:
        with sqlite3.connect(DB_PATH) as connection:
            row = connection.execute("SELECT id FROM users WHERE stripe_customer_id=?", (customer_id,)).fetchone()
            user_id = row[0] if row else None
    if not user_id:
        return False
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """UPDATE users SET stripe_customer_id=?, stripe_subscription_id=?,
               subscription_status=?, subscription_plan=COALESCE(?, subscription_plan)
               WHERE id=?""",
            (customer_id, subscription.get("id"), subscription.get("status", "free"), plan, int(user_id)),
        )
    return True

@app.route("/vendor-access")
def vendor_access():
    if not session.get("user_id"):
        return redirect(url_for("register", next=url_for("vendor_access")))
    user=get_current_user()
    if _can_use_vendor_tools(user):
        return redirect(url_for("vendors"))
    return redirect(url_for("pricing", vendor="1"))

@app.route("/pricing")
@login_required
def pricing():
    user = get_current_user()
    vendor_flow = request.args.get("vendor") == "1"
    active = user["subscription_status"] in {"active", "trialing"}
    body = render_template_string("""
    <section class="pro-hero"><p class="tag">{% if vendor_flow %}VENDOR ACCESS{% else %}FLIPFINDER PRO{% endif %}</p><h1>{% if vendor_flow %}Choose Monthly or Yearly to unlock vendors{% else %}Flip smarter with Pro{% endif %}</h1>
    <p>Choose the plan that fits your flipping schedule. Every option unlocks the same Pro experience.</p>
    {% if active %}<p class="profit" style="margin-top:20px">Active plan: {{ (user.subscription_plan or 'Pro')|title }}</p>{% endif %}
    {% if not ready %}<p class="error">Stripe is not fully configured in .env.</p>{% endif %}</section>
    <section class="pro-grid">{% for key,label,price,period,note,popular in plans %}{% if not vendor_flow or key in ['monthly','yearly'] %}
    <div class="card pro-plan {% if popular %}popular{% endif %}">{% if popular %}<div class="pro-badge">MOST POPULAR</div>{% endif %}
    <p class="tag">{{ label }}</p><div class="pro-price">{{ price }}</div><div class="pro-period">{{ period }}</div><p>{{ note }}</p>
    <ul class="pro-list"><li>Marketplace search workspace</li><li>Deal analysis and profit tracking</li><li>Featured iPhone advertising</li><li>Saved account preferences</li>{% if key in ['monthly', 'yearly'] %}<li><strong>Full Vendor access and public business profile</strong></li><li>Advertise your business, products, website and services</li>{% else %}<li>Vendor profiles require Monthly or Yearly</li>{% endif %}</ul>
    <form method="post" action="/create-checkout-session/{{ key }}"><button class="submit-button pro-button" {% if not ready %}disabled{% endif %}>Choose {{ label }}</button></form></div>{% endif %}{% endfor %}</section>
    <div style="text-align:center;margin:-35px 0 60px"><p class="warning">Stripe Checkout shows and charges the final payment amount. Displayed conversions are estimates.</p></div>
    """, user=user, active=active, ready=stripe_ready(), vendor_flow=vendor_flow, plans=[
      ("weekly","Weekly","A$4","per week","Flexible access without a long commitment.",False),
      ("monthly","Monthly + Vendor Access","A$14","per month","Includes everything in Weekly, plus full Vendor access. Create a public vendor profile, advertise your business across FlipFinder, display your location, contact details, website and services, and help customers discover your products.",True),
      ("yearly","Yearly","A$140","per year","Best long-term value. Includes a public vendor profile so customers can discover your business, location, contact details, website and services, while saving A$28 compared with twelve monthly payments.",False)])
    return page("FlipFinder Pro", body)

@app.route("/create-checkout-session/<plan>", methods=["POST"])
@login_required
def create_checkout_session(plan):
    if plan not in STRIPE_PRICES:
        return page("Stripe Error", "<div class='container'><div class='card'><p class='error'>Unknown subscription plan.</p></div></div>"), 404
    if not stripe.api_key:
        return page("Stripe Error", "<div class='container'><div class='card'><p class='error'>Missing STRIPE_SECRET_KEY in .env.</p></div></div>"), 400
    try:
        price_id = resolve_stripe_price(plan)
    except (RuntimeError, stripe.error.StripeError) as exc:
        return page("Stripe Setup Error", f"<div class='container'><div class='card'><h1>Stripe plan needs attention</h1><p class='error'>{exc}</p><a class='main-button' href='/pricing'>Back to Plans</a></div></div>"), 400
    user = get_current_user()
    if not user["email_verified"]:
        return redirect(url_for("verify_email_notice"))
    if not user["email"] or user["email"].endswith("@local.invalid"):
        return page("Email Required", "<div class='container'><div class='card'><p class='error'>Add a real email address in Settings first.</p></div></div>"), 400
    try:
        checkout_args = {
            "mode": "subscription",
            "line_items": [{"price": price_id, "quantity": 1}],
            "client_reference_id": str(user["id"]),
            "metadata": {"flipfinder_user_id": str(user["id"]), "plan": plan},
            "subscription_data": {"metadata": {"flipfinder_user_id": str(user["id"]), "plan": plan}},
            "success_url": url_for("subscription_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": url_for("pricing", _external=True) + "?checkout=cancelled",
        }
        if user["stripe_customer_id"]:
            checkout_args["customer"] = user["stripe_customer_id"]
        else:
            checkout_args["customer_email"] = user["email"]
        checkout = stripe.checkout.Session.create(**checkout_args)
        return redirect(checkout.url, code=303)
    except stripe.error.StripeError as exc:
        return page("Stripe Error", f"<div class='container'><div class='card'><p class='error'>{exc}</p></div></div>"), 400

@app.route("/subscription-success")
@login_required
def subscription_success():
    checkout_id = request.args.get("session_id", "")
    try:
        checkout = stripe_to_dict(stripe.checkout.Session.retrieve(checkout_id))
        if str(checkout.get("client_reference_id")) != str(session["user_id"]):
            return "Wrong FlipFinder account", 403
        subscription = stripe_to_dict(stripe.Subscription.retrieve(checkout.get("subscription")))
        if checkout.get("status") != "complete":
            return page("Payment Pending", "<div class='container'><div class='card'><p class='error'>Stripe Checkout is not complete yet.</p></div></div>"), 400
        plan = stripe_to_dict(checkout.get("metadata")).get("plan")
        sync_subscription(subscription, session["user_id"], plan)
    except stripe.error.StripeError as exc:
        return page("Stripe Error", f"<div class='container'><div class='card'><p class='error'>{exc}</p></div></div>"), 400
    return page("FlipFinder Pro", "<div class='container'><div class='card'><h1>Welcome to FlipFinder Pro</h1><p>Your subscription is active.</p><a class='main-button' href='/account'>Open Account</a></div></div>")

@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        return "Missing webhook secret", 503
    payload = request.get_data(cache=False)
    signature = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe_to_dict(stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET))
    except (ValueError, stripe.error.SignatureVerificationError):
        return "Invalid webhook", 400

    event_type = event.get("type", "")
    event_data = stripe_to_dict(event.get("data"))
    stripe_object = stripe_to_dict(event_data.get("object"))
    try:
        if event_type == "checkout.session.completed":
            subscription_id = stripe_object.get("subscription")
            metadata = stripe_to_dict(stripe_object.get("metadata"))
            user_id = metadata.get("flipfinder_user_id") or stripe_object.get("client_reference_id")
            if metadata.get("purpose") == "listing_promotion" and stripe_object.get("payment_status") == "paid":
                listing_id = metadata.get("listing_id")
                with sqlite3.connect(DB_PATH) as connection:
                    connection.execute("UPDATE iphone_comments SET featured=1 WHERE id=? AND user_id=?", (listing_id, user_id))
                    connection.execute("UPDATE listing_promotions SET status='paid',paid_at=CURRENT_TIMESTAMP WHERE stripe_session_id=?", (stripe_object.get("id"),))
            elif subscription_id:
                subscription = stripe_to_dict(stripe.Subscription.retrieve(subscription_id))
                sync_subscription(subscription, user_id, metadata.get("plan"))
        elif event_type in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}:
            sync_subscription(stripe_object)
        elif event_type in {"invoice.paid", "invoice.payment_failed"}:
            subscription_id = stripe_object.get("subscription")
            if subscription_id:
                sync_subscription(stripe_to_dict(stripe.Subscription.retrieve(subscription_id)))
    except stripe.error.StripeError as exc:
        print("STRIPE WEBHOOK ERROR:", exc)
        return "Stripe API error", 500
    return "", 200


@app.route("/logout")
def logout():
    session.clear(); return redirect("/")


@app.route("/billing-portal", methods=["POST"])
@login_required
def billing_portal():
    user=get_current_user()
    if not user["email_verified"]:
        return redirect(url_for("verify_email_notice"))
    if not stripe.api_key:
        return page("Billing unavailable","<div class='container'><div class='card'><p class='error'>Stripe is not configured.</p></div></div>"),503
    if not user["stripe_customer_id"]:
        return redirect(url_for("pricing"))
    try:
        portal=stripe.billing_portal.Session.create(customer=user["stripe_customer_id"],return_url=url_for("account",_external=True))
        return redirect(portal.url,code=303)
    except stripe.error.StripeError as exc:
        return page("Billing portal error",f"<div class='container'><div class='card'><h1>Could not open subscription management</h1><p class='error'>{exc}</p><a class='main-button' href='/account'>Back to profile</a></div></div>"),400

@app.route("/cancel-subscription", methods=["POST"])
@login_required
def cancel_subscription_portal():
    user=get_current_user()
    if not user["email_verified"]:
        return redirect(url_for("verify_email_notice"))
    if not user["stripe_customer_id"] or not user["stripe_subscription_id"]:
        return redirect(url_for("pricing"))
    if not stripe.api_key:
        return page("Cancellation unavailable","<div class='container'><div class='card'><p class='error'>Stripe is not configured.</p></div></div>"),503
    try:
        portal=stripe.billing_portal.Session.create(
            customer=user["stripe_customer_id"],
            return_url=url_for("account",_external=True),
            flow_data={
                "type":"subscription_cancel",
                "subscription_cancel":{"subscription":user["stripe_subscription_id"]},
                "after_completion":{"type":"redirect","redirect":{"return_url":url_for("account",_external=True)+"?cancellation=updated"}},
            },
        )
        return redirect(portal.url,code=303)
    except stripe.error.StripeError as exc:
        return page("Cancellation portal error",f"<div class='container'><div class='card'><h1>Could not open cancellation</h1><p class='error'>{exc}</p><a class='main-button' href='/account'>Back to profile</a></div></div>"),400

@app.route("/account")
@login_required
def account():
    user=get_current_user(); deals=load_deals(); total,average,best=load_statistics()
    body=render_template_string("""<div class="container"><div class="card wide-card"><p class="tag">MY ACCOUNT</p><h1>Hello, {{ user.username }}</h1>
    <p>{{ 'No email added' if (not user.email or user.email.endswith('@local.invalid')) else user.email }} · {{ user.phone or 'No phone added' }}</p>
    <div class="features"><div class="card feature-card"><h2>{{ total }}</h2><p>Saved Deals</p></div><div class="card feature-card"><h2>A${{ '%.2f'|format(average) }}</h2><p>Average Profit</p></div><div class="card feature-card"><h2>A${{ '%.2f'|format(best) }}</h2><p>Best Profit</p></div></div>
    <div class="card" style="margin:20px 0"><p class="tag">SUBSCRIPTION</p><h2>{{ plan_label }}</h2><p>Status: <strong>{{ status_label }}</strong></p>{% if has_subscription %}<p>Use secure Stripe subscription management to change plans, cancel, update payment details, and view invoices.</p><div class="onboarding-actions"><form method="post" action="{{ url_for('billing_portal') }}"><button class="submit-button">Change plan or manage billing</button></form><form method="post" action="{{ url_for('cancel_subscription_portal') }}"><button class="submit-button cancel-subscription-button" style="background:#c73b4d!important;border:2px solid #ff8794!important;color:#fff!important;display:inline-block!important;opacity:1!important;visibility:visible!important">Cancel subscription</button></form></div><p class="locale-note">Cancellation opens a secure Stripe confirmation page. Your Stripe portal setting controls whether access ends immediately or at the end of the paid billing period.</p>{% else %}<p>No paid Stripe subscription is currently linked to this profile.</p><div class="onboarding-actions"><a class="main-button" href="/pricing">View available plans</a><button class="submit-button cancel-subscription-button" type="button" disabled title="A paid Stripe subscription must be linked first" style="background:#8b3440!important;border:2px solid #b96570!important;color:#fff!important;display:inline-block!important;opacity:.65!important;visibility:visible!important;cursor:not-allowed">Cancel subscription</button></div><p class="locale-note">The cancellation button activates after Stripe links a paid subscription to this account.</p>{% endif %}</div>
    <a class="main-button" href="/settings">Settings</a><a class="main-button" href="/two-factor-setup">Security</a><a class="main-button" href="/analyse">Analyse Deal</a>{% if vendor_access %}<a class="main-button" href="/vendor-profile">Create or Edit Vendor Profile</a>{% endif %}
    <h2>Recent Deals</h2>{% if deals %}{% for phone,profit,rating in deals %}<div class="history-item"><strong>{{ phone }}</strong><p>{{ rating }} · A${{ '%.2f'|format(profit) }}</p></div>{% endfor %}{% else %}<p>No saved deals yet.</p>{% endif %}</div></div>""",user=user,deals=deals,total=total,average=average,best=best,vendor_access=_can_use_vendor_tools(user),show_apple_chart=bool(user and user["show_apple_chart"]),chart_accent=(user["accent_color"] if user else "#347cff"),plan_label=(user["subscription_plan"] or "Free").title(),status_label=(user["subscription_status"] or "free").replace("_"," ").title(),has_subscription=bool(user["stripe_customer_id"] and user["stripe_subscription_id"]))
    return page("My Account | FlipFinder",body)


@app.route("/change-password",methods=["GET","POST"])
@login_required
def change_password():
    user=get_current_user(); error=""; message=""
    if request.method=="POST":
        current=request.form.get("current_password",""); new=request.form.get("new_password",""); confirm=request.form.get("confirm_password","")
        if not check_password_hash(user["password_hash"],current): error="Current password is incorrect."
        elif len(new)<8: error="New password must be at least 8 characters."
        elif new!=confirm: error="New passwords do not match."
        elif check_password_hash(user["password_hash"],new): error="Choose a password different from the current password."
        else:
            with sqlite3.connect(DB_PATH) as connection: connection.execute("UPDATE users SET password_hash=? WHERE id=?",(generate_password_hash(new),user["id"]))
            try: send_security_notice(user["email"],"Your FlipFinder password was changed","Your FlipFinder password was changed. If this was not you, use the password reset page immediately.")
            except (RuntimeError,OSError,smtplib.SMTPException): pass
            message="Password changed successfully."
    body=render_template_string("""<div class='container'><div class='card auth-card'><p class='tag'>ACCOUNT SECURITY</p><h1>Change password</h1>{% if error %}<p class='error'>{{ error }}</p>{% endif %}{% if message %}<p style='color:#5ee59d'>{{ message }}</p>{% endif %}<form method='post'><label>Current password</label><input type='password' name='current_password' required autocomplete='current-password'><label>New password</label><input type='password' name='new_password' minlength='8' required autocomplete='new-password'><label>Confirm new password</label><input type='password' name='confirm_password' minlength='8' required autocomplete='new-password'><button class='submit-button'>Verify and change password</button></form></div></div>""",error=error,message=message)
    return page("Change Password | FlipFinder",body)

@app.route("/change-email",methods=["GET","POST"])
@login_required
def change_email():
    user=get_current_user(); error=""; message=""
    if request.method=="POST":
        password=request.form.get("current_password",""); new_email=request.form.get("new_email","").strip().lower()
        if not check_password_hash(user["password_hash"],password): error="Current password is incorrect."
        elif "@" not in new_email: error="Enter a valid email address."
        elif new_email==user["email"]: error="Enter a different email address."
        else:
            try:
                with sqlite3.connect(DB_PATH) as connection: connection.execute("UPDATE users SET email=?,email_verified=0 WHERE id=?",(new_email,user["id"]))
                issue_email_verification(user["id"],new_email)
                try: send_security_notice(user["email"],"Your FlipFinder email was changed",f"The email on your FlipFinder account was changed to {new_email}. If this was not you, reset your password immediately.")
                except (RuntimeError,OSError,smtplib.SMTPException): pass
                return redirect(url_for("verify_email_notice"))
            except sqlite3.IntegrityError: error="That email address is already linked to another account."
            except (RuntimeError,OSError,smtplib.SMTPException) as exc: error=f"The email was changed, but verification could not be sent: {exc}"
    body=render_template_string("""<div class='container'><div class='card auth-card'><p class='tag'>VERIFIED CHANGE</p><h1>Change email</h1><p>The new address must be verified before protected account actions continue.</p>{% if error %}<p class='error'>{{ error }}</p>{% endif %}<form method='post'><label>Current password</label><input type='password' name='current_password' required autocomplete='current-password'><label>New email</label><input type='email' name='new_email' required autocomplete='email'><button class='submit-button'>Verify password and change email</button></form></div></div>""",error=error)
    return page("Change Email | FlipFinder",body)

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user=get_current_user(); message=""
    if request.method=="POST":
        accent=request.form.get("accent_color","#347cff")
        theme=request.form.get("theme","dark")
        background_style=request.form.get("background_style","midnight")
        if background_style not in {"midnight","ocean","sunset","forest","graphite"}: background_style="midnight"
        show_apple_chart=1 if request.form.get("show_apple_chart") else 0
        if theme not in {"dark","light"}: theme="dark"
        def setting_colour(name,default):
            value=request.form.get(name,default)
            return value if re.fullmatch(r"#[0-9a-fA-F]{6}",value or "") else default
        surface_color=setting_colour("surface_color","#141d31"); text_color=setting_colour("text_color","#eef4ff"); muted_color=setting_colour("muted_color","#aebbd0"); nav_color=setting_colour("nav_color","#0b1220"); button_text_color=setting_colour("button_text_color","#ffffff")
        compact=1 if request.form.get("compact_cards") else 0
        default_page=request.form.get("default_page","/")
        if default_page not in {"/","/account","/ebay-search","/facebook-search","/gumtree-search","/market-values"}: default_page="/"
        quick=1 if request.form.get("show_quick_search") else 0
        market=1 if request.form.get("show_market_values") else 0
        email=user["email"]
        email_changed=False
        phone=request.form.get("phone","").strip() or None
        country_code=request.form.get("country_code",current_country_code())
        if country_code not in COUNTRIES: country_code="AU"
        language=request.form.get("language",current_language())
        if language not in LANGUAGES: language=COUNTRIES[country_code]["language"]
        search_location=request.form.get("search_location","").strip() or COUNTRIES[country_code]["default_location"]
        if not email and not phone: message="Keep at least one email address or phone number."
        else:
            try:
                with sqlite3.connect(DB_PATH) as connection:
                    connection.execute("UPDATE users SET email=?,phone=?,accent_color=?,theme=?,compact_cards=?,default_page=?,show_quick_search=?,show_market_values=?,country_code=?,language=?,currency_code=?,search_location=?,background_style=?,show_apple_chart=?,surface_color=?,text_color=?,muted_color=?,nav_color=?,button_text_color=?,email_verified=CASE WHEN ? THEN 0 ELSE email_verified END WHERE id=?",(email,phone,accent,theme,compact,default_page,quick,market,country_code,language,COUNTRIES[country_code]["currency"],search_location,background_style,show_apple_chart,surface_color,text_color,muted_color,nav_color,button_text_color,email_changed,session["user_id"]))
                    session["country_code"]=country_code; session["language"]=language; session["search_location"]=search_location
                message="Settings saved."; user=get_current_user()
            except sqlite3.IntegrityError: message="That email or phone number is already used."
    body=render_template_string("""<div class="container"><div class="card wide-card"><p class="tag">SETTINGS</p><h1>Personalise FlipFinder</h1>{% if message %}<p class="warning">{{ message }}</p>{% endif %}
    <form method="post"><h2>Contact</h2><label>Email</label><input name="email" type="email" value="{{ '' if (not user.email or user.email.endswith('@local.invalid')) else user.email }}"><label>Phone number</label><input name="phone" type="tel" value="{{ user.phone or '' }}">
    <h2>Account security</h2><a class="main-button" href="/change-password">Change password</a><a class="main-button hero-secondary" href="/change-email">Change email</a><p class="tag">EMAIL STATUS</p><p>{% if user.email_verified %}<strong style="color:#5ee59d">Verified</strong>{% else %}<strong class="warning">Not verified</strong> · <a href="/verify-email">Verify now</a>{% endif %}</p><h2>Country, language and currency</h2><label>Country</label><select name="country_code">{% for code,item in countries.items() %}<option value="{{ code }}" {% if code==user.country_code %}selected{% endif %}>{{ item.flag }} {{ item.name }} · {{ item.currency }}</option>{% endfor %}</select><label>Nearby phone search location</label><input name="search_location" value="{{ user.search_location or countries[user.country_code].default_location }}" placeholder="City, suburb, state or postcode"><label>Website language</label><select name="language">{% for code,name in languages.items() %}<option value="{{ code }}" {% if code==user.language %}selected{% endif %}>{{ name }}</option>{% endfor %}</select><h2>Appearance</h2><div class="choice-grid"><label>Accent and buttons<input name="accent_color" type="color" value="{{ user.accent_color }}"></label><label>Cards and menus<input name="surface_color" type="color" value="{{ user.surface_color }}"></label><label>Main text<input name="text_color" type="color" value="{{ user.text_color }}"></label><label>Secondary text<input name="muted_color" type="color" value="{{ user.muted_color }}"></label><label>Navigation bar<input name="nav_color" type="color" value="{{ user.nav_color }}"></label><label>Button text<input name="button_text_color" type="color" value="{{ user.button_text_color }}"></label></div><label>Background</label><select name="background_style">{% for value,label in backgrounds %}<option value="{{ value }}" {% if user.background_style==value %}selected{% endif %}>{{ label }}</option>{% endfor %}</select><label>Theme</label><select name="theme"><option value="dark" {% if user.theme=='dark' %}selected{% endif %}>Dark</option><option value="light" {% if user.theme=='light' %}selected{% endif %}>Light</option></select><label><input style="width:auto" type="checkbox" name="compact_cards" {% if user.compact_cards %}checked{% endif %}> Compact listing cards</label>
    <h2>Homepage</h2><label><input style="width:auto" type="checkbox" name="show_apple_chart" {% if user.show_apple_chart %}checked{% endif %}> Show Apple product-value chart</label><a class="main-button hero-secondary" href="/tutorial">Replay website tutorial</a><label>Default page after login</label><select name="default_page">{% for value,label in pages %}<option value="{{ value }}" {% if user.default_page==value %}selected{% endif %}>{{ label }}</option>{% endfor %}</select><label><input style="width:auto" type="checkbox" name="show_quick_search" {% if user.show_quick_search %}checked{% endif %}> Show marketplace shortcuts</label><label><input style="width:auto" type="checkbox" name="show_market_values" {% if user.show_market_values %}checked{% endif %}> Show market values shortcut</label><button class="submit-button">Save Settings</button></form></div></div>""",user=user,message=message,pages=[("/","Home"),("/account","My Account"),("/ebay-search","eBay Search"),("/facebook-search","Facebook Search"),("/gumtree-search","Gumtree Search"),("/market-values","Market Values")],countries=COUNTRIES,languages=LANGUAGES,backgrounds=[("midnight","Midnight"),("ocean","Ocean Blue"),("sunset","Sunset"),("forest","Forest"),("graphite","Graphite")])
    return page("Settings | FlipFinder",body)


@app.route("/two-factor-setup", methods=["GET", "POST"])
@login_required
def two_factor_setup():
    user=get_current_user(); secret=session.get("new_2fa_secret") or generate_totp_secret(); session["new_2fa_secret"]=secret
    error=""; recovery_plain=[]
    if request.method=="POST":
        if request.form.get("action")=="disable":
            with sqlite3.connect(DB_PATH) as connection: connection.execute("UPDATE users SET two_factor_enabled=0,two_factor_secret=NULL,recovery_codes=NULL WHERE id=?",(user["id"],))
            session.pop("new_2fa_secret",None); return redirect("/account")
        code=request.form.get("code","")
        if verify_totp(secret,code):
            recovery_plain=[secrets.token_hex(4).upper() for _ in range(8)]
            hashes=[hash_recovery(code) for code in recovery_plain]
            with sqlite3.connect(DB_PATH) as connection: connection.execute("UPDATE users SET two_factor_enabled=1,two_factor_secret=?,recovery_codes=? WHERE id=?",(secret,json.dumps(hashes),user["id"]))
            session.pop("new_2fa_secret",None)
        else: error="The authenticator code was not valid."
    uri=f"otpauth://totp/FlipFinder:{quote_plus(user['username'])}?secret={secret}&issuer=FlipFinder"
    body=render_template_string("""<div class="container"><div class="card wide-card"><p class="tag">ACCOUNT SECURITY</p><h1>Two-step verification</h1>
    {% if user.two_factor_enabled and not recovery %}<p>Two-step verification is enabled.</p><form method="post"><input type="hidden" name="action" value="disable"><button class="clear-button">Disable two-step verification</button></form>
    {% elif recovery %}<h2>Save these recovery codes now</h2><p>Each code works once.</p><pre style="white-space:pre-wrap">{{ recovery|join('  ') }}</pre><a class="main-button" href="/account">Done</a>
    {% else %}<p>Add this secret to an authenticator app, then enter its six-digit code.</p><p><strong>Secret:</strong> {{ secret }}</p><details><summary>Show setup URI</summary><code style="word-break:break-all">{{ uri }}</code></details>{% if error %}<p class="error">{{ error }}</p>{% endif %}<form method="post"><label>Authenticator code</label><input name="code" required><button class="submit-button">Enable two-step verification</button></form>{% endif %}</div></div>""",user=user,secret=secret,uri=uri,error=error,recovery=recovery_plain)
    return page("Security | FlipFinder",body)



@app.route("/accept-terms", methods=["POST"])
def accept_terms():
    response = app.response_class(response=json.dumps({"accepted": True}), status=200, mimetype="application/json")
    response.set_cookie("flipfinder_terms", "2026-10-07", max_age=31536000, httponly=True, samesite="Lax")
    return response

@app.route("/terms")
def terms():
    template="""
    <article class="card terms-page">
      <p class="tag">FLIPFINDER LEGAL</p><h1>Terms and Conditions</h1>
      <p><strong>Effective date:</strong> 7 October 2026</p>
      <p>These Terms and Conditions govern access to and use of FlipFinder. By selecting “Accept and Continue”, creating an account, purchasing a subscription, or continuing to use FlipFinder, the user agrees to these terms.</p>
      <p class="warning"><strong>Draft notice:</strong> This is a practical starter document, not legal advice. It should be reviewed by an Australian lawyer before public launch.</p>

      <h2>1. What FlipFinder provides</h2>
      <p>FlipFinder provides marketplace-search links and integrations, vendor discovery, price alerts, serial-number routing to Apple, market-value estimates, reporting tools, account features and deal-calculation tools. Results are provided for general information and research only.</p>

      <h2>2. No guarantee of listings or results</h2>
      <p>Listings, prices, availability, product descriptions, seller details, shipping, taxes, minimum order quantities, warranties and external results may change, be delayed, incomplete or inaccurate. Users must independently inspect devices, confirm ownership and condition, verify seller identity, calculate all costs, and decide whether a transaction is suitable.</p>

      <h2>3. Third-party services</h2>
      <p>FlipFinder may link to or display information from third-party marketplaces, vendors, payment providers and support services. FlipFinder does not control third-party websites, products, sellers, policies or availability. The user must follow each third party's own terms. References to Apple, eBay, Facebook Marketplace, Gumtree, Temu, Alibaba and vendor names do not imply sponsorship, endorsement or affiliation.</p>

      <h2>4. Serial-number and device checks</h2>
      <p>The serial-check feature validates basic input format and directs the user to Apple's official coverage service. Coverage information alone does not prove authenticity, ownership, unlock status, Activation Lock status, finance status, repair history, device condition or whether a device is safe to purchase. Users must not publish serial numbers or other sensitive device identifiers.</p>

      <h2>5. Accounts and security</h2>
      <p>Users are responsible for accurate account information, password security and activity under their account. Users must promptly report suspected unauthorised access. FlipFinder may suspend access where reasonably necessary to protect users, the service or third parties.</p>

      <h2>6. Subscriptions and payments</h2>
      <p>Paid plans, billing periods, recurring charges and prices are shown before checkout. Subscriptions renew until cancelled in accordance with the checkout terms. Nothing in these terms excludes rights that cannot lawfully be excluded under the Australian Consumer Law.</p>

      <h2>7. Vendor profiles and user content</h2>
      <p>Users submitting vendor profiles, comments, reports or other content confirm that the content is accurate to the best of their knowledge, lawful, and does not infringe another person's rights. FlipFinder may moderate, hide or remove content and may investigate reports. Inclusion in the vendor directory is not an endorsement.</p>

      <h2>8. Acceptable use</h2>
      <p>Users must not use FlipFinder to break the law, deceive others, distribute harmful code, harass users, submit counterfeit or misleading listings, interfere with security, bypass access controls, scrape or mass-download the service without permission, or overload the website or connected services.</p>

      <h2>9. Intellectual property</h2>
      <p>FlipFinder branding, original website content, design and code are protected by applicable intellectual-property laws. Third-party trademarks remain the property of their respective owners. Users may use FlipFinder only for ordinary personal or authorised business use and must not reproduce or resell FlipFinder content without permission.</p>

      <h2>10. Privacy</h2>
      <p>FlipFinder may process account details, vendor-profile information, reports, alert settings, transaction identifiers and technical data required to operate the service. Users should not submit unnecessary sensitive information. A separate Privacy Policy should be prepared before public launch to explain collection, use, storage, disclosure, access and correction practices.</p>

      <h2>11. Availability and changes</h2>
      <p>FlipFinder may update, interrupt, restrict or discontinue features. These terms may be updated by publishing a revised version and requesting acceptance again where appropriate.</p>

      <h2>12. Liability and consumer rights</h2>
      <p>To the maximum extent permitted by law, FlipFinder is not responsible for decisions, transactions, losses or disputes arising from third-party listings, sellers, vendors, external services, estimates or user-provided content. This limitation does not exclude, restrict or modify consumer guarantees, rights or remedies that cannot lawfully be excluded.</p>

      <h2>13. Governing law</h2>
      <p>These terms are governed by the laws applying in Western Australia, Australia. The parties submit to the courts with jurisdiction in Western Australia, subject to any rights that apply under mandatory law.</p>

      <h2>14. Contact and complaints</h2>
      <p>Questions, complaints and legal notices should be sent using the contact details published by the FlipFinder operator. Reports about vendors or listings can also be submitted through the Report system.</p>

      <div class="terms-actions"><a class="main-button" href="/">Return to FlipFinder</a></div>
    </article>
    """
    return page("Terms and Conditions | FlipFinder", render_template_string(template))

@app.route("/set-locale", methods=["POST"])
def set_locale():
    code=request.form.get("country_code", current_country_code()).upper()
    if code not in COUNTRIES: code="AU"
    language=request.form.get("language", COUNTRIES[code]["language"])
    if language not in LANGUAGES: language=COUNTRIES[code]["language"]
    search_location=request.form.get("search_location","").strip() or COUNTRIES[code]["default_location"]
    session["country_code"]=code; session["language"]=language; session["search_location"]=search_location
    if session.get("user_id"):
        with sqlite3.connect(DB_PATH) as connection:
            connection.execute("UPDATE users SET country_code=?,language=?,currency_code=?,search_location=? WHERE id=?",(code,language,COUNTRIES[code]["currency"],search_location,session["user_id"]))
    return redirect(request.referrer or url_for("home"))

@app.route("/")
def home():
    user = get_current_user()
    show_quick = not user or user["show_quick_search"]
    show_market = not user or user["show_market_values"]
    body = render_template_string("""
    {% if not paid %}
    <div class="preview-pill">Preview: <span id="previewPillClock">15:00</span></div>
    <div class="preview-modal-backdrop" id="previewModal"><div class="preview-modal">
    <p class="tag">WELCOME TO FLIPFINDER</p><h2>Explore before choosing Pro</h2>
    <p>Browse FlipFinder for fifteen minutes. After the preview, Pro is required to use the app tools.</p>
    <div class="preview-clock" id="previewModalClock">15:00 remaining</div>
    <div class="preview-actions"><button class="main-button" type="button" onclick="document.getElementById('previewModal').style.display='none'">Continue Browsing</button><a class="main-button hero-secondary" href="/pricing">View Pro Plans</a>{% if not session.get('user_id') %}<a class="main-button hero-secondary" href="/register">Create Account</a>{% endif %}</div>
    <p style="font-size:13px;color:#99a6c0">Plans renew automatically until cancelled. Pricing and billing periods are shown before checkout.</p>
    </div></div>
    <script>(function(){const total={{ preview_seconds|int }};const started={{ preview_started_ms|int }};const modal=document.getElementById('previewModal'),a=document.getElementById('previewModalClock'),b=document.getElementById('previewPillClock');if(!modal||!a||!b)return;setTimeout(()=>modal.style.display='flex',900);function tick(){const left=Math.max(0,total-Math.floor((Date.now()-started)/1000));const minutes=String(Math.floor(left/60)).padStart(2,'0');const seconds=String(left%60).padStart(2,'0');a.textContent=minutes+':'+seconds+' remaining';b.textContent=minutes+':'+seconds;if(left<=0){window.location.replace({{ ('/pricing' if session.get('user_id') else '/register')|tojson }});return;}window.setTimeout(tick,250);}tick();})();</script>
    {% endif %}

    <main class="hero">
<p class="tag">IPHONE STORE SEARCH HUB</p>
      <h1>Search all your iPhone stores from one home page.</h1>
      <p>Choose a marketplace below, find iPhone listings, then use the separate FlipFinder tools area when you want alerts, market values, reports or deal analysis.</p>
      <div class="hero-buttons"><a class="main-button hero-primary" href="#stores">Browse Stores</a><a class="main-button hero-secondary" href="/workspace">Open FlipFinder Tools</a></div>
    </main>

    <section class="home-section" id="stores">
      <p class="tag">ALL STORES</p><h2>Choose where you want to search</h2>
      <p class="home-section-intro">Store searches are grouped here so the shopping side stays separate from account and analysis features.</p>
      <div class="store-grid">
        <div class="card store-card"><div><span class="store-badge">MARKETPLACE</span><h3>eBay Australia</h3><p>Search iPhones and other Apple devices by model, year, price and condition.</p></div><a class="main-button" href="/ebay-search">Search eBay</a></div>
        <div class="card store-card"><div><span class="store-badge">LOCAL</span><h3>Facebook Marketplace</h3><p>Search local marketplace listings using your configured Facebook search source.</p></div><a class="main-button" href="/facebook-search">Search Facebook</a></div>
        <div class="card store-card"><div><span class="store-badge">LOCAL</span><h3>Gumtree</h3><p>Look for local iPhone listings and compare nearby asking prices.</p></div><a class="main-button" href="/gumtree-search">Search Gumtree</a></div>
        <div class="card store-card"><div><span class="store-badge">MARKETPLACE</span><h3>Temu</h3><p>Search Temu using the iPhone-only filter that removes accessories and other brands.</p></div><a class="main-button" href="/temu-search">Search Temu</a></div>
        <div class="card store-card"><div><span class="store-badge">WHOLESALE</span><h3>Alibaba</h3><p>Find wholesale iPhone listings with supplier, MOQ and Trade Assurance information.</p></div><a class="main-button" href="/alibaba-search">Search Alibaba</a></div>
        <div class="card store-card"><div><span class="store-badge">MONTHLY OR YEARLY</span><h3>Vendor Directory</h3><p>Discover vendor profiles and sourcing businesses with an eligible subscription.</p></div><a class="main-button" href="/vendor-access">Unlock Vendors</a></div>
      </div>
    </section>

    <section class="home-section" id="discover">
      <p class="tag">DISCOVER FLIPFINDER</p><h2>From finding an iPhone to making a smarter decision</h2>
      <p class="home-section-intro">The stores stay together above, while this discovery area explains what you can do after finding a listing.</p>
      <div class="features">
        <div class="card step-card"><div class="step-number">1</div><h3>Find</h3><p>Choose a store and search for the iPhone model you want.</p><a class="main-button" href="#stores">Browse Stores</a></div>
        <div class="card step-card"><div class="step-number">2</div><h3>Compare</h3><p>Check asking prices and compare the listing against current market values.</p><a class="main-button" href="/market-values">View Market Values</a></div>
        <div class="card step-card"><div class="step-number">3</div><h3>Analyse</h3><p>Enter the purchase price, condition, battery health, fees and shipping to estimate profit.</p><a class="main-button" href="/analyse">Analyse a Deal</a></div>
        <div class="card step-card"><div class="step-number">4</div><h3>Track</h3><p>Create a price alert and check for iPhones listed under your target price.</p><a class="main-button" href="/price-alerts">Open Price Alerts</a></div>
      </div>
    </section>

    <section class="home-section">
      <p class="tag">QUICK DISCOVERY</p><h2>Explore the rest of FlipFinder</h2>
      <div class="features">
        <div class="card quick-card"><div class="quick-icon">$</div><h3>Deal Calculator</h3><p>Estimate potential profit before buying.</p><a class="main-button" href="/analyse">Open</a></div>
        <div class="card quick-card"><div class="quick-icon">↗</div><h3>Market Values</h3><p>Compare current asking-price estimates.</p><a class="main-button" href="/market-values">View</a></div>
        <div class="card quick-card"><div class="quick-icon">◎</div><h3>Vendor Directory</h3><p>Available with Monthly or Yearly membership.</p><a class="main-button" href="/vendor-access">Unlock Vendors</a></div>
        <div class="card quick-card"><div class="quick-icon">⚙</div><h3>Tools Dashboard</h3><p>Open account, reports, alerts and reselling tools.</p><a class="main-button" href="/workspace">Open</a></div>
      </div>
    </section>


    {% if show_apple_chart %}<section class="home-section apple-chart-card"><p class="tag">APPLE PRODUCT VALUES</p><h2>Live-updating Apple value chart</h2><p class="home-section-intro">This chart refreshes from FlipFinder's current market-value dataset every 30 seconds.</p><div class="card chart-wrap"><canvas id="appleValueChart"></canvas></div><small id="chartUpdated"></small></section><script src="https://cdn.jsdelivr.net/npm/chart.js"></script><script>(function(){let chart;async function load(){const r=await fetch('/apple-chart-data',{cache:'no-store'}),d=await r.json();if(!chart){chart=new Chart(document.getElementById('appleValueChart'),{type:'line',data:{labels:d.labels,datasets:[{label:'Estimated value (AUD)',data:d.values,borderColor:'{{ chart_accent }}',backgroundColor:'{{ chart_accent }}33',fill:true,tension:.3}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#dfe8f7'}}},scales:{x:{ticks:{color:'#aebbd0'}},y:{ticks:{color:'#aebbd0'}}}}});}else{chart.data.labels=d.labels;chart.data.datasets[0].data=d.values;chart.update();}document.getElementById('chartUpdated').textContent='Updated '+new Date(d.updated).toLocaleTimeString();}load();setInterval(load,30000);})();</script>{% endif %}
    <section class="pro-home-strip"><div><p class="tag">FLIPFINDER SIDE</p><h2>Need analysis or account tools?</h2><p>Deal analysis, market values, price alerts, reports, selling, subscriptions and account settings now live in their own tools area.</p></div><a class="main-button hero-primary" href="/workspace">Open Tools Dashboard</a></section>
    """, show_quick=show_quick, show_market=show_market, paid=_subscription_is_active(user), preview_seconds=PREVIEW_SECONDS, preview_started_ms=int(session.get("preview_started_at_v3", int(time.time()))) * 1000,country=COUNTRIES[current_country_code()],countries=COUNTRIES,country_code=current_country_code(),language=current_language(),vendor_access=_can_use_vendor_tools(user),show_apple_chart=bool(user and user["show_apple_chart"]),chart_accent=(user["accent_color"] if user else "#347cff"))
    response = app.make_response(page("FlipFinder", body))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

@app.route("/account-home", methods=["GET", "POST"])
@login_required
def account_home():
    message = ""
    if request.method == "POST":
        action = request.form.get("action", "add")
        if action == "delete":
            with sqlite3.connect(DB_PATH) as connection:
                connection.execute("DELETE FROM inventory_sales WHERE id=? AND user_id=?", (request.form.get("sale_id"), session["user_id"]))
            message = "Entry deleted."
        else:
            name = request.form.get("product_name", "").strip()[:120]
            period = request.form.get("period", "week")
            if period not in {"week", "month", "year"}: period = "week"
            try:
                stock = max(0, int(request.form.get("quantity_in_stock", 0)))
                sold = max(0, int(request.form.get("quantity_sold", 0)))
                cost = max(0, float(request.form.get("unit_cost", 0)))
                price = max(0, float(request.form.get("unit_sale_price", 0)))
            except ValueError:
                stock=sold=0; cost=price=0; message="Enter valid numbers."
            if not message and name and price >= 0:
                with sqlite3.connect(DB_PATH) as connection:
                    connection.execute("INSERT INTO inventory_sales(user_id,product_name,quantity_in_stock,quantity_sold,unit_cost,unit_sale_price,period) VALUES(?,?,?,?,?,?,?)", (session["user_id"],name,stock,sold,cost,price,period))
                message = "Sales and stock entry saved."
            elif not message:
                message = "Enter a product name."
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory=sqlite3.Row
        rows=connection.execute("SELECT * FROM inventory_sales WHERE user_id=? ORDER BY id DESC",(session["user_id"],)).fetchall()
    total_revenue=total_cost=actual_profit=weekly_profit=0.0
    inventory_units=0
    for row in rows:
        unit_profit=float(row["unit_sale_price"])-float(row["unit_cost"])
        sold=int(row["quantity_sold"]); stock=int(row["quantity_in_stock"])
        total_revenue += sold*float(row["unit_sale_price"])
        total_cost += sold*float(row["unit_cost"])
        actual_profit += sold*unit_profit
        inventory_units += stock
        period_factor={"week":1.0,"month":1/4.345,"year":1/52}.get(row["period"],1.0)
        weekly_profit += sold*unit_profit*period_factor
    monthly_profit=weekly_profit*4.345
    yearly_profit=weekly_profit*52
    remaining_profit=sum(int(r["quantity_in_stock"])*(float(r["unit_sale_price"])-float(r["unit_cost"])) for r in rows)
    chart_values=[round(weekly_profit,2),round(monthly_profit,2),round(yearly_profit,2)]
    template="""
    <main class='hero'><p class='tag'>ACCOUNT HOME</p><h1>Profit and Stock Dashboard</h1><p>Enter actual sales and current stock. FlipFinder calculates recorded profit and projections from your selected sales period.</p></main>
    {% if message %}<div class='container'><div class='card wide-card'><p class='warning'>{{ message }}</p></div></div>{% endif %}
    <section class='metric-grid'><div class='card metric-card'><p>Recorded Revenue</p><div class='metric-value'>A${{ '%.2f'|format(total_revenue) }}</div></div><div class='card metric-card'><p>Recorded Profit</p><div class='metric-value'>A${{ '%.2f'|format(actual_profit) }}</div></div><div class='card metric-card'><p>Stock Units</p><div class='metric-value'>{{ inventory_units }}</div></div><div class='card metric-card'><p>Potential Stock Profit</p><div class='metric-value'>A${{ '%.2f'|format(remaining_profit) }}</div></div></section>
    <section class='dashboard-grid'><div class='card'><h2>Add Sales and Stock</h2><form method='post'><input type='hidden' name='action' value='add'><label>Product</label><input name='product_name' placeholder='Example: iPhone 13 128GB' required><label>Quantity currently in stock</label><input name='quantity_in_stock' type='number' min='0' value='0' required><label>Quantity sold in this period</label><input name='quantity_sold' type='number' min='0' value='0' required><label>Cost per item (AUD)</label><input name='unit_cost' type='number' min='0' step='0.01' required><label>Sale price per item (AUD)</label><input name='unit_sale_price' type='number' min='0' step='0.01' required><label>Sales period</label><select name='period'><option value='week'>Week</option><option value='month'>Month</option><option value='year'>Year</option></select><button class='submit-button'>Save Entry</button></form></div>
    <div class='card'><h2>Projected Profit</h2><p>Projection assumes the entered sales pace continues consistently.</p><canvas id='profitChart' class='profit-chart' aria-label='Projected profit chart'></canvas><div class='features' style='padding:20px 0'><div><strong>Week</strong><p>A${{ '%.2f'|format(weekly_profit) }}</p></div><div><strong>Month</strong><p>A${{ '%.2f'|format(monthly_profit) }}</p></div><div><strong>Year</strong><p>A${{ '%.2f'|format(yearly_profit) }}</p></div></div></div></section>
    <section class='container'><div class='card wide-card'><h2>Your Entries</h2><div class='sales-table-wrap'><table class='sales-table'><thead><tr><th>Product</th><th>Stock</th><th>Sold</th><th>Unit cost</th><th>Sale price</th><th>Period</th><th>Profit</th><th></th></tr></thead><tbody>{% for r in rows %}<tr><td>{{ r.product_name }}</td><td>{{ r.quantity_in_stock }}</td><td>{{ r.quantity_sold }}</td><td>A${{ '%.2f'|format(r.unit_cost) }}</td><td>A${{ '%.2f'|format(r.unit_sale_price) }}</td><td>{{ r.period|title }}</td><td>A${{ '%.2f'|format(r.quantity_sold*(r.unit_sale_price-r.unit_cost)) }}</td><td><form method='post'><input type='hidden' name='action' value='delete'><input type='hidden' name='sale_id' value='{{ r.id }}'><button class='nav-button' style='color:#ff8b8b'>Delete</button></form></td></tr>{% else %}<tr><td colspan='8'>No sales or stock entries yet.</td></tr>{% endfor %}</tbody></table></div></div></section>
    <script>(function(){const canvas=document.getElementById('profitChart');if(!canvas)return;const values={{ chart_values|tojson }},labels=['Week','Month','Year'];function draw(){const dpr=window.devicePixelRatio||1,w=canvas.clientWidth,h=canvas.clientHeight;canvas.width=w*dpr;canvas.height=h*dpr;const c=canvas.getContext('2d');c.scale(dpr,dpr);c.clearRect(0,0,w,h);const pad=48,max=Math.max(...values,1),plotH=h-pad*2,barW=Math.min(90,(w-pad*2)/6),gap=(w-pad*2)/3;c.font='14px sans-serif';c.textAlign='center';labels.forEach((label,i)=>{const x=pad+gap*i+gap/2,bh=(values[i]/max)*plotH,y=h-pad-bh,g=c.createLinearGradient(0,y,0,h-pad);g.addColorStop(0,'#347cff');g.addColorStop(1,'#7857ff');c.fillStyle=g;c.fillRect(x-barW/2,y,barW,bh);c.fillStyle='#dbe7ff';c.fillText('A$'+values[i].toLocaleString(undefined,{maximumFractionDigits:2}),x,Math.max(22,y-10));c.fillStyle='#9facbf';c.fillText(label,x,h-18);});}draw();window.addEventListener('resize',draw);})();</script>
    """
    return page("Account Home | FlipFinder",render_template_string(template,rows=rows,total_revenue=total_revenue,actual_profit=actual_profit,inventory_units=inventory_units,remaining_profit=remaining_profit,weekly_profit=weekly_profit,monthly_profit=monthly_profit,yearly_profit=yearly_profit,chart_values=chart_values))

@app.route("/promote-listing/<int:listing_id>", methods=["POST"])
@login_required
def promote_listing(listing_id):
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory=sqlite3.Row
        listing=connection.execute("SELECT * FROM iphone_comments WHERE id=? AND user_id=?",(listing_id,session["user_id"])).fetchone()
    if not listing: return "Listing not found",404
    if listing["featured"]: return redirect(url_for("iphone_community"))
    if not stripe.api_key:
        return page("Promotion unavailable","<div class='container'><div class='card'><p class='error'>Stripe is not configured.</p></div></div>"),503
    line_item={"quantity":1}
    if STRIPE_PRICE_PROMOTION.startswith("price_"):
        promotion_price = stripe_to_dict(stripe.Price.retrieve(STRIPE_PRICE_PROMOTION))
        if not promotion_price.get("active") or promotion_price.get("type") != "one_time" or promotion_price.get("currency") != "aud" or int(promotion_price.get("unit_amount") or 0) != 199:
            return page("Promotion setup error", "<div class='container'><div class='card'><p class='error'>STRIPE_PRICE_PROMOTION must be an active one-time AUD A$1.99 price.</p></div></div>"), 503
        line_item["price"] = STRIPE_PRICE_PROMOTION
    elif STRIPE_PRICE_PROMOTION.startswith("prod_"):
        prices = stripe.Price.list(product=STRIPE_PRICE_PROMOTION, active=True, type="one_time", limit=100)
        promotion_price = next((stripe_to_dict(price) for price in prices.data if stripe_to_dict(price).get("currency") == "aud" and int(stripe_to_dict(price).get("unit_amount") or 0) == 199), None)
        if not promotion_price:
            return page("Promotion setup error", "<div class='container'><div class='card'><p class='error'>The Stripe product needs an active one-time AUD A$1.99 price.</p></div></div>"), 503
        line_item["price"] = promotion_price["id"]
    else:
        line_item["price_data"]={"currency":"aud","unit_amount":199,"product_data":{"name":"FlipFinder listing promotion"}}
    try:
        checkout=stripe.checkout.Session.create(
            mode="payment",
            line_items=[line_item],
            client_reference_id=str(session["user_id"]),
            customer_email=(get_current_user()["email"] if get_current_user() and not get_current_user()["email"].endswith("@local.invalid") else None),
            metadata={"purpose":"listing_promotion","listing_id":str(listing_id),"flipfinder_user_id":str(session["user_id"])},
            payment_intent_data={"metadata":{"purpose":"listing_promotion","listing_id":str(listing_id),"flipfinder_user_id":str(session["user_id"])}},
            success_url=url_for("promotion_success",_external=True)+"?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=url_for("iphone_community",_external=True)+"?promotion=cancelled",
        )
        with sqlite3.connect(DB_PATH) as connection:
            connection.execute("INSERT INTO listing_promotions(listing_id,user_id,stripe_session_id) VALUES(?,?,?)",(listing_id,session["user_id"],checkout.id))
        return redirect(checkout.url,code=303)
    except stripe.error.StripeError as exc:
        return page("Promotion payment error",f"<div class='container'><div class='card'><h1>Could not open Stripe Checkout</h1><p class='error'>{exc}</p><a class='main-button' href='/iphone-community'>Back to listing</a></div></div>"),400

@app.route("/promotion-success")
@login_required
def promotion_success():
    session_id=request.args.get("session_id","")
    try:
        checkout=stripe_to_dict(stripe.checkout.Session.retrieve(session_id))
    except stripe.error.StripeError as exc:
        return page("Payment check error",f"<div class='container'><div class='card'><p class='error'>{exc}</p></div></div>"),400
    metadata=stripe_to_dict(checkout.get("metadata"))
    if checkout.get("payment_status")!="paid" or metadata.get("purpose")!="listing_promotion" or str(metadata.get("flipfinder_user_id"))!=str(session["user_id"]):
        return page("Payment pending","<div class='container'><div class='card'><p class='error'>Promotion payment is not confirmed.</p></div></div>"),400
    listing_id=int(metadata["listing_id"])
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("UPDATE iphone_comments SET featured=1 WHERE id=? AND user_id=?",(listing_id,session["user_id"]))
        connection.execute("UPDATE listing_promotions SET status='paid',paid_at=CURRENT_TIMESTAMP WHERE stripe_session_id=?",(session_id,))
    return page("Promotion active","<div class='container'><div class='card'><h1>Your listing is promoted</h1><p>The A$1.99 one-time payment was confirmed. The product is now a featured sponsored advertisement across FlipFinder.</p><a class='main-button' href='/iphone-community'>View Listing</a></div></div>")

@app.route("/serial-check", methods=["GET", "POST"])
def serial_check():
    serial = "".join(ch for ch in request.values.get("serial", "").upper().strip() if ch.isalnum())[:20]
    error = ""
    apple_url = ""
    if request.method == "POST":
        if len(serial) < 8:
            error = "Enter the full serial number shown in Settings or on the device packaging."
        else:
            apple_url = "https://checkcoverage.apple.com/au/en/?sn=" + quote_plus(serial)
    template="""
    <div class='container'><div class='card wide-card'>
      <p class='tag'>APPLE SERIAL CHECK</p><h1>Check an iPhone serial number</h1>
      <p>FlipFinder checks the entry format, then sends the serial number to Apple's official coverage page. Apple may ask for a verification code before showing results.</p>
      <form method='post'><label>Apple serial number</label><input name='serial' value='{{ serial }}' maxlength='20' autocomplete='off' spellcheck='false' placeholder='Enter serial number' required><button class='submit-button'>Continue to Apple</button></form>
      {% if error %}<p class='error'>{{ error }}</p>{% endif %}
      {% if apple_url %}<div class='promo-box'><h2>Serial ready</h2><p>Continue to Apple to view available coverage information.</p><a class='main-button' href='{{ apple_url }}' target='_blank' rel='noopener noreferrer'>Open Apple Coverage Check</a></div>{% endif %}
      <p class='warning'><strong>Important:</strong> Coverage information alone does not prove that an iPhone is genuine, unlocked, fully functional, paid off or safe to buy. Compare the serial shown in Settings with the device and packaging, inspect the phone, and check Activation Lock with the seller before paying.</p>
      <details><summary>Where to find the serial number</summary><p>On the iPhone, open Settings, then General, then About. Do not post a serial number publicly.</p></details>
    </div></div>
    """
    return page("Apple Serial Check | FlipFinder",render_template_string(template,serial=serial,error=error,apple_url=apple_url))

@app.route("/workspace")
def workspace():
    template="""
    <main class='hero'><p class='tag'>FLIPFINDER TOOLS</p><h1>Your account and reselling workspace</h1><p>Everything that is not a store search is organised here.</p></main>
    <section class='home-section'><div class='features'>
      <div class='card quick-card'><h3>Profit Dashboard</h3><p>Track stock, sales and projected weekly, monthly and yearly profit.</p><a class='main-button' href='/account-home'>Open Dashboard</a></div>
      <div class='card quick-card'><h3>Analyse a Deal</h3><p>Estimate profit using purchase price, condition, fees and shipping.</p><a class='main-button' href='/analyse'>Open Calculator</a></div>
      <div class='card quick-card'><h3>Market Values</h3><p>Review current iPhone asking-price estimates.</p><a class='main-button' href='/market-values'>View Values</a></div>
      <div class='card quick-card'><h3>Apple Serial Check</h3><p>Enter a serial number, then continue to Apple's official coverage checker.</p><a class='main-button' href='/serial-check'>Check Serial</a></div>
      <div class='card quick-card'><h3>Price Alerts</h3><p>Save a maximum price and check for cheap iPhones.</p><a class='main-button' href='/price-alerts'>Open Alerts</a></div>
      <div class='card quick-card'><h3>Sell an iPhone</h3><p>Use the iPhone community and selling area.</p><a class='main-button' href='/iphone-community'>Open Selling</a></div>
      <div class='card quick-card'><h3>Reports</h3><p>Review reports you submitted about vendors or listings.</p><a class='main-button' href='/reports'>Open Reports</a></div>
      <div class='card quick-card'><h3>My Account</h3><p>Manage your profile, preferences and subscription.</p><a class='main-button' href='{% if session.get("user_id") %}/account{% else %}/login{% endif %}'>Open Account</a></div>
      <div class='card quick-card'><h3>Pro Plans</h3><p>Review FlipFinder subscription options.</p><a class='main-button' href='/pricing'>View Plans</a></div>
    </div></section>
    """
    return page("FlipFinder Tools",render_template_string(template))

@app.route("/ebay-search", methods=["GET"])
def ebay_search():
    device_type = request.args.get("device_type", "iphone").strip()
    model = request.args.get("model", "").strip()
    year = request.args.get("year", "").strip()
    max_price = request.args.get("max_price", "").strip()
    condition = request.args.get("condition", "").strip()
    cards, error_message = [], ""

    query = " ".join(part for part in [model, year, "unlocked smartphone"] if part)
    gumtree_url = f"https://www.gumtree.com.au/s-{quote_plus(query)}/k0"

    if model:
        try:
            for item in search_ebay(model, year, max_price, condition, device_type):
                cards.append({
                    "title": item.get("title", "Untitled listing"),
                    "price": item.get("price", {}).get("value", "Unknown"),
                    "currency": item.get("price", {}).get("currency", ""),
                    "condition": item.get("condition", "Not specified"),
                    "url": item.get("itemWebUrl", "#"),
                    "image": item.get("image", {}).get("imageUrl", "")
                })
        except (requests.RequestException, RuntimeError) as error:
            error_message = str(error)

    template = """
    <div class="container"><div class="card">
      <p class="tag">EBAY AUSTRALIA</p><h1>Find Apple Devices on eBay</h1>
      <form method="get">
        <label>Device type</label><select name="device_type">{% for key, item in devices.items() %}<option value="{{ key }}" {% if key == device_type %}selected{% endif %}>{{ item.label }}</option>{% endfor %}</select>
        <label>Model or specifications</label><input name="model" value="{{ model }}" placeholder="Example: iPhone 13 128GB" required>
        <label>Year</label><select name="year"><option value="">Any year</option>{% for option in years %}<option value="{{ option }}" {% if option == year %}selected{% endif %}>{{ option }}</option>{% endfor %}</select>
        <label>Maximum price (AUD)</label><input name="max_price" type="number" min="1" step="0.01" value="{{ max_price }}">
        <label>Condition</label><select name="condition"><option value="">Any</option><option value="NEW" {% if condition=='NEW' %}selected{% endif %}>New</option><option value="USED" {% if condition=='USED' %}selected{% endif %}>Used</option></select>
        <button class="submit-button">Find Phones on eBay</button>
      </form>
      {% if model %}<a class="main-button" href="/facebook-search?model={{ model|urlencode }}&location={{ local_location|urlencode }}&max_price={{ max_price|urlencode }}&condition={{ condition|urlencode }}">Search Facebook too</a><a class="main-button" href="{{ gumtree_url }}" target="_blank">Open Gumtree</a>{% endif %}
    </div></div>
    {% if error_message %}<div class="container"><div class="card"><p class="error">{{ error_message }}</p></div></div>{% elif model and not cards %}<div class="container"><div class="card"><p>No matching Apple devices found.</p></div></div>{% endif %}
    <div class="container">{% for item in cards %}<div class="card">{% if item.image %}<img class="listing-image" src="{{ item.image }}">{% endif %}<h3>{{ item.title }}</h3><p class="listing-price">{{ item.currency }} ${{ item.price }}</p><p>{{ item.condition }}</p><a class="main-button" href="{{ item.url }}" target="_blank">View Listing</a></div>{% endfor %}</div>
    """
    body = render_template_string(template, device_type=device_type, model=model, year=year, max_price=max_price, condition=condition, years=[str(v) for v in range(2026,2006,-1)], cards=cards, error_message=error_message, gumtree_url=gumtree_url, devices=DEVICE_CONFIG, local_location=current_search_location())
    return page("eBay Search | FlipFinder", body)


@app.route("/facebook-search", methods=["GET"])
def facebook_search():
    device_type = request.args.get("device_type", "iphone").strip()
    model = request.args.get("model", "").strip()
    location = request.args.get("location", current_search_location()).strip()
    max_price = request.args.get("max_price", "").strip()
    condition = request.args.get("condition", "").strip()
    cards, error_message = [], ""

    if model and location:
        try:
            cards = search_facebook_marketplace(model, location, max_price, condition, device_type)
        except (requests.RequestException, RuntimeError, ValueError) as error:
            error_message = str(error)

    template = """
    <div class="container"><div class="card">
      <p class="tag">FACEBOOK MARKETPLACE</p><h1>Find Apple Devices on Facebook</h1>
      <form method="get">
        <label>Device type</label><select name="device_type">{% for key, item in devices.items() %}<option value="{{ key }}" {% if key == device_type %}selected{% endif %}>{{ item.label }}</option>{% endfor %}</select>
        <label>Model or specifications</label><input name="model" value="{{ model }}" placeholder="Example: M2 16GB 512GB" required>
        <label>City, state or country</label><input name="location" value="{{ location }}" placeholder="Example: Sydney, NSW, Australia" required>
        <label>Maximum price (local currency)</label><input name="max_price" type="number" min="1" step="1" value="{{ max_price }}">
        <label>Condition</label><select name="condition"><option value="">Any condition</option><option value="NEW" {% if condition=='NEW' %}selected{% endif %}>New</option><option value="USED" {% if condition=='USED' %}selected{% endif %}>Used</option></select>
        <button class="submit-button">Search Facebook Marketplace</button>
      </form>
      <p class="warning">Each search runs the paid Apify Actor.</p>
    </div></div>
    {% if error_message %}<div class="container"><div class="card"><h2>Facebook connection error</h2><p class="error">{{ error_message }}</p></div></div>{% elif model and not cards %}<div class="container"><div class="card"><p>No matching Facebook listings found.</p></div></div>{% endif %}
    <div class="container">{% for item in cards %}<div class="card">{% if item.image %}<img class="listing-image" src="{{ item.image }}">{% endif %}<h3>{{ item.title }}</h3><p class="listing-price">A${{ '%.2f'|format(item.price) }}</p><p><strong>Condition:</strong> {{ item.condition }}</p><p><strong>Location:</strong> {{ item.location }}</p>{% if item.published %}<p><strong>Published:</strong> {{ item.published }}</p>{% endif %}<a class="main-button" href="{{ item.url }}" target="_blank">View on Facebook</a></div>{% endfor %}</div>
    """
    body = render_template_string(template, device_type=device_type, devices=DEVICE_CONFIG, model=model, location=location, max_price=max_price, condition=condition, cards=cards, error_message=error_message)
    return page("Facebook Search | FlipFinder", body)


def search_gumtree(model, location="", max_price="", device_type="iphone"):
    api_token = os.getenv("APIFY_API_TOKEN")
    if not api_token:
        raise RuntimeError("Missing APIFY_API_TOKEN in .env")

    device = DEVICE_CONFIG.get(device_type, DEVICE_CONFIG["iphone"])
    query = " ".join(part for part in [device["label"], model.strip()] if part)

    run_input = {
        "mode": "search",
        "urls": [],
        "category": "electronics-computer",
        "location": location.strip(),
        "keywords": query,
        "sortBy": "date",
        "radius": 65,
        "fetchDetails": False,
        "maxItems": 20,
    }
    if max_price:
        run_input["maxPrice"] = int(float(max_price))

    response = requests.post(
        "https://api.apify.com/v2/acts/abotapi~gumtree-au-scraper/run-sync-get-dataset-items",
        headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
        json=run_input,
        params={"format": "json", "clean": "true", "maxItems": 20, "maxTotalChargeUsd": 0.10},
        timeout=300,
    )
    response.raise_for_status()

    blocked = ["wanted", "buying", "box only", "back glass", "replacement", "repair", "case", "cover", "protector", "charger", "cable", "screen only", "parts only"]
    results = []
    for item in response.json():
        title = str(item.get("title", "")).strip()
        if not title or any(word in title.lower() for word in blocked):
            continue
        try:
            price = float(item.get("price", 0) or 0)
        except (TypeError, ValueError):
            price = 0
        if price <= 0 or (max_price and price > float(max_price)):
            continue
        images = item.get("images") or []
        image = ""
        if images:
            first = images[0]
            image = first.get("url", "") if isinstance(first, dict) else str(first)
        results.append({
            "title": title,
            "price": price,
            "location": item.get("location") or item.get("locationArea") or "Not specified",
            "state": item.get("locationState", ""),
            "url": item.get("url", "#"),
            "image": image,
            "age": item.get("age", ""),
        })
    return results[:20]


@app.route("/gumtree-search", methods=["GET"])
def gumtree_search():
    device_type = request.args.get("device_type", "iphone").strip()
    model = request.args.get("model", "").strip()
    location = request.args.get("location", current_search_location()).strip()
    max_price = request.args.get("max_price", "").strip()
    cards, error_message = [], ""
    if model:
        try:
            cards = search_gumtree(model, location, max_price, device_type)
        except (requests.RequestException, RuntimeError, ValueError) as error:
            error_message = str(error)

    template = """
    <div class="container"><div class="card">
      <p class="tag">GUMTREE AUSTRALIA</p><h1>Find Apple Devices on Gumtree</h1>
      <form method="get">
        <label>Device type</label><select name="device_type">{% for key, item in devices.items() %}<option value="{{ key }}" {% if key == device_type %}selected{% endif %}>{{ item.label }}</option>{% endfor %}</select>
        <label>Model or specifications</label><input name="model" value="{{ model }}" placeholder="Example: MacBook Air M2 16GB" required>
        <label>Suburb, city, region or state</label><input name="location" value="{{ location }}" placeholder="Example: Sydney, NSW" required>
        <label>Maximum price (AUD)</label><input name="max_price" type="number" min="1" step="1" value="{{ max_price }}">
        <button class="submit-button">Search Gumtree</button>
      </form><p class="warning">Each search runs the paid Apify Gumtree Actor.</p>
    </div></div>
    {% if error_message %}<div class="container"><div class="card"><h2>Gumtree connection error</h2><p class="error">{{ error_message }}</p></div></div>
    {% elif model and not cards %}<div class="container"><div class="card"><p>No matching Gumtree listings found.</p></div></div>{% endif %}
    <div class="container">{% for item in cards %}<div class="card">{% if item.image %}<img class="listing-image" src="{{ item.image }}" alt="Gumtree listing image">{% endif %}<h3>{{ item.title }}</h3><p class="listing-price">A${{ '%.2f'|format(item.price) }}</p><p><strong>Location:</strong> {{ item.location }}{% if item.state %}, {{ item.state }}{% endif %}</p>{% if item.age %}<p><strong>Listed:</strong> {{ item.age }}</p>{% endif %}<a class="main-button" href="{{ item.url }}" target="_blank" rel="noopener noreferrer">View on Gumtree</a></div>{% endfor %}</div>
    """
    body = render_template_string(template, device_type=device_type, devices=DEVICE_CONFIG, model=model, location=location, max_price=max_price, cards=cards, error_message=error_message)
    return page("Gumtree Search | FlipFinder", body)


@app.errorhandler(413)
def upload_too_large(error):
    return page("Image Too Large | FlipFinder", "<div class='container'><div class='card'><h1>Image too large</h1><p>Please upload a PNG, JPG or WEBP image smaller than 8 MB.</p><a class='main-button' href='/iphone-community'>Go Back</a></div></div>"), 413


def allowed_iphone_image(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


@app.route("/iphone-community", methods=["GET", "POST"])
def iphone_community():
    error_message = ""
    if request.method == "POST":
        if not session.get("user_id"):
            return redirect(url_for("register", next=url_for("iphone_community")))

        comment = request.form.get("comment", "").strip()
        image = request.files.get("image")
        image_filename = None
        featured = 0

        if len(comment) < 15:
            error_message = "Include the model, condition, price and buyer instructions."
        elif len(comment) > 1500:
            error_message = "Your comment must be 1,500 characters or fewer."
        elif not image or not image.filename:
            error_message = "Please add a picture of the iPhone."
        elif not allowed_iphone_image(image.filename):
            error_message = "Pictures must be PNG, JPG, JPEG or WEBP files."
        else:
            extension = secure_filename(image.filename).rsplit(".", 1)[1].lower()
            image_filename = f"{session['user_id']}_{secrets.token_hex(16)}.{extension}"
            image.save(os.path.join(app.config["UPLOAD_FOLDER"], image_filename))
            try:
                with sqlite3.connect(DB_PATH) as connection:
                    connection.execute(
                        "INSERT INTO iphone_comments (user_id, comment, image_filename, featured) VALUES (?, ?, ?, ?)",
                        (session["user_id"], comment, image_filename, featured),
                    )
            except Exception:
                saved_path = os.path.join(app.config["UPLOAD_FOLDER"], image_filename)
                if os.path.exists(saved_path):
                    os.remove(saved_path)
                raise
            return redirect(url_for("iphone_community"))

    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        comments = connection.execute("""
            SELECT iphone_comments.id, iphone_comments.user_id, iphone_comments.comment,
                   iphone_comments.image_filename, iphone_comments.featured, iphone_comments.created_at, users.username
            FROM iphone_comments JOIN users ON users.id = iphone_comments.user_id
            ORDER BY iphone_comments.featured DESC, iphone_comments.id DESC LIMIT 100
        """).fetchall()

    template = """
    <div class="container"><div class="card wide-card sell-hero"><p class="sell-kicker">FLIPFINDER COMMUNITY MARKET</p><h1>Give Your iPhone a Better Second Story</h1><p>Turn an unused iPhone into an eye-catching listing. Add a strong photo, write a polished description and connect directly with interested buyers across the FlipFinder community.</p></div></div>
    <div class="container"><div class="community-layout">
      <div class="card community-form-card">
        <p class="tag">COMMUNITY MARKETPLACE</p><h1>Sell Your iPhone</h1>
        <p>Create a polished listing that helps buyers understand the device at a glance. Include the exact model, storage, battery health, cosmetic condition, asking price and meetup area.</p>
        <div class="selling-notice">Never post passwords, verification codes, banking details or your home address. Meet safely and inspect the phone before paying.</div>
        {% if error_message %}<p class="error">{{ error_message }}</p>{% endif %}
        {% if session.get('user_id') %}
          <form method="post" enctype="multipart/form-data">
            <label>iPhone picture</label><input name="image" type="file" accept="image/png,image/jpeg,image/webp" required>
            <p class="upload-help">Required. PNG, JPG, JPEG or WEBP only. Maximum size: 8 MB.</p>
            <label>Listing description</label><textarea name="comment" rows="10" maxlength="1500" required placeholder="Selling iPhone 13 128GB&#10;Price: A$520&#10;Condition: Good&#10;Battery health: 88%&#10;Location: Perth&#10;Comment if interested.">{{ request.form.get('comment', '') }}</textarea>
            <div class="promo-box"><strong>Publishing is free</strong><p class="upload-help">Your product will be published normally. Promotion is a separate A$1.99 one-time purchase available after publishing.</p></div>
            <button class="submit-button" type="submit">Publish My iPhone Listing</button>
          </form>
        {% else %}
          <h3>Create an account before uploading</h3><p>You must have a FlipFinder account and be logged in before uploading a picture or publishing a selling comment.</p>
          <a class="main-button" href="{{ url_for('register', next=url_for('iphone_community')) }}">Create Account</a><a class="main-button hero-secondary" href="{{ url_for('login', next=url_for('iphone_community')) }}">Log In</a>
        {% endif %}
      </div>
      <div><div class="card wide-card"><p class="tag">IPHONES FOR SALE</p><h2>Recent Community Listings</h2></div><div class="comment-list">
        {% if comments %}{% for item in comments %}<article id="listing-{{ item.id }}" class="card comment-card {% if item.featured %}featured-listing{% endif %}">{% if item.featured %}<span class="featured-badge">FEATURED AD</span>{% endif %}<div class="comment-header"><div class="comment-user"><div class="comment-avatar">{{ item.username[0]|upper }}</div><div><strong>{{ item.username }}</strong><div class="comment-date">{{ item.created_at }}</div></div></div>{% if session.get('user_id') == item.user_id %}<div style="display:flex;gap:8px;flex-wrap:wrap">{% if not item.featured %}<form method="post" action="{{ url_for('promote_listing', listing_id=item.id) }}"><button class="main-button" type="submit">Advertise sitewide for A$1.99</button></form>{% endif %}<form method="post" action="{{ url_for('delete_iphone_comment', comment_id=item.id) }}"><button class="nav-button" type="submit" style="color:#ff8b8b">Delete</button></form></div>{% endif %}</div>{% if item.image_filename %}<img class="comment-photo" src="{{ url_for('iphone_comment_image', filename=item.image_filename) }}" alt="Picture uploaded for {{ item.username }} iPhone listing" loading="lazy">{% endif %}<p class="comment-text">{{ item.comment }}</p></article>{% endfor %}
        {% else %}<div class="card comment-card"><h3>No listings yet</h3><p>Be the first account holder to advertise an iPhone.</p></div>{% endif %}
      </div></div>
    </div></div>
    """
    return page("Sell an iPhone | FlipFinder", render_template_string(template, comments=comments, error_message=error_message, paid=_subscription_is_active(get_current_user())))


@app.route("/iphone-community/images/<path:filename>")
def iphone_comment_image(filename):
    safe_name = secure_filename(filename)
    if safe_name != filename:
        return "Invalid filename", 400
    return send_from_directory(app.config["UPLOAD_FOLDER"], safe_name)


@app.route("/iphone-community/delete/<int:comment_id>", methods=["POST"])
@login_required
def delete_iphone_comment(comment_id):
    image_filename = None
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        item = connection.execute(
            "SELECT image_filename FROM iphone_comments WHERE id = ? AND user_id = ?",
            (comment_id, session["user_id"]),
        ).fetchone()
        if item:
            image_filename = item["image_filename"]
            connection.execute(
                "DELETE FROM iphone_comments WHERE id = ? AND user_id = ?",
                (comment_id, session["user_id"]),
            )
    if image_filename:
        image_path = os.path.join(app.config["UPLOAD_FOLDER"], secure_filename(image_filename))
        if os.path.isfile(image_path):
            os.remove(image_path)
    return redirect(url_for("iphone_community"))


@app.route("/vendors")
def vendors():
    user = get_current_user()
    if not _can_use_vendor_tools(user):
        return redirect(url_for("vendor_access"))
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        vendor_rows = connection.execute("SELECT vendors.*, users.username FROM vendors JOIN users ON users.id=vendors.user_id ORDER BY vendors.business_name").fetchall()
    curated = [
        {"business_name":"Alibaba", "location":"Global wholesale marketplace", "description":"Wholesale supplier marketplace. Check MOQ, supplier history, Trade Assurance, shipping, warranty and landed cost before ordering.", "website":"https://www.alibaba.com/", "contact":"Use Alibaba supplier contact tools."},
        {"business_name":"Temu Australia", "location":"Online marketplace", "description":"Marketplace used by FlipFinder's Temu iPhone search. Verify the listing, seller, condition, shipping and returns before purchasing.", "website":"https://www.temu.com/au", "contact":"Use Temu support and listing seller details."},
        {"business_name":"Vinted Resells Ltd", "location":"United Kingdom / online", "description":"Reselling supplier-links and starter-kit store. Review product terms and suitability before purchasing.", "website":"https://vintedresells.com/", "contact":"Use the vendor website contact options."},
        {"business_name":"The Resell Suite", "location":"Online", "description":"Online reseller stock store. Review authenticity, returns, delivery and product details before purchasing.", "website":"https://resellsuite.com/", "contact":"Use the vendor website customer support."},
        {"business_name":"AutoDS", "location":"Online / global", "description":"Dropshipping platform for product discovery, store creation, sourcing, order fulfilment and tracking automation. Review its plans, supported suppliers and channel limitations before connecting a store.", "website":"https://www.autods.com/lp/general/?utm_source=bing&utm_medium=cpc&utm_campaign=ds_bng_srch_eng_competitors_maxconv_trial&utm_content=kw_inventorysource_exphbr&utm_term=www.inventorysource.com&msclkid=3640db5682f91cd4a4d3b19e50ed776e", "contact":"Use AutoDS support and account tools."},
        {"business_name":"Worldwide Promotional Products & Merchandise", "location":"Australia", "description":"Promotional merchandise source covering bags, clothing and headwear, drinkware, events, gifts, office products and writing products. Check branding, quantity, turnaround and delivery details before ordering.", "website":"https://promo.worldwide.com.au/?utm_source=bing&utm_medium=cpc&utm_campaign=promo&msclkid=508d188c8fbb1ecbc31572445dec594a", "contact":"Use the Worldwide website contact and account options."},
        {"business_name":"OzMobiles Wholesale", "location":"Australia / Melbourne", "description":"Australian wholesale option for companies purchasing devices in bulk. Its wholesale information says eligibility is B2B and requires a minimum purchase of five devices per month.", "website":"https://ozmobiles.com.au/pages/wholesale", "contact":"Submit wholesale requirements through the OzMobiles website."},
        {"business_name":"Skyline Mobile - Second Hand iPhones", "location":"Australia / Sydney dispatch", "description":"Second-hand iPhones offered for wholesale to resellers, retailers and repair shops, with device testing, cleaning, unlocking and grading information listed by the supplier.", "website":"https://skylinemobile.com.au/collections/iphones", "contact":"Use Skyline Mobile trade account and support options."},
        {"business_name":"Merkandi Australia - Wholesale iPhone Lots", "location":"International B2B marketplace", "description":"B2B marketplace page listing wholesale iPhone lots from multiple sellers. Carefully verify each seller, device grade, lock status, warranty, minimum order, freight and import costs.", "website":"https://merkandi.com.au/wholesale/iphone-lots", "contact":"Contact the individual seller through Merkandi."},
        {"business_name":"HK Refurbished Stock - Wholesale iPhones", "location":"Hong Kong / international", "description":"Wholesale source focused on refurbished iPhones and bulk orders. Confirm grading, parts, battery health, network compatibility, warranty, freight and import costs before purchasing.", "website":"https://hk-refurbished-stock.com/buy-iphones-wholesale/", "contact":"Use the supplier website contact options."},
        {"business_name":"WeSellCellular", "location":"United States / international shipping", "description":"Wholesale source for accurately graded pre-owned phones and devices, including iPhones, Samsung phones, iPads and Apple Watches. Account setup is required for pricing and stock-list access. Verify grading, customs, shipping, returns and regional compatibility before ordering.", "website":"https://www.wesellcellular.com/", "contact":"Set up an account or use WeSellCellular contact and support options."},
        {"business_name":"Accio - Apple Suppliers List", "location":"Worldwide supplier directory", "description":"Supplier-discovery page listing Apple, iPhone and electronics vendors with filters such as verified supplier, minimum order quantity, supplier experience and ratings. Treat each supplier as a separate business and independently verify authenticity, grading, warranty, payment protection, shipping and import costs.", "website":"https://www.accio.com/supplier/apple-list-of-suppliers", "contact":"Contact individual suppliers through the Accio listing page."},
        {"business_name":"TG Wireless - Wholesale Apple iPhones", "location":"United States / wholesale", "description":"Wholesale Apple iPhone supplier listing new, refurbished and used unlocked iPhones across multiple stock grades. Product pricing is shown to members only. Verify grading, minimum orders, network compatibility, warranty, shipping, customs and returns before purchasing.", "website":"https://tgwireless.com/wholesale-apple/", "contact":"Apply or sign in through the TG Wireless website for member pricing and purchasing."},
    ]
    template = """
    <div class="container"><div class="card wide-card sell-hero"><p class="sell-kicker">VENDOR DIRECTORY</p><h1>Find Vendors</h1><p>Browse sourcing platforms and public profiles created by FlipFinder members. Inclusion is not an endorsement. Verify authenticity, prices, shipping, returns and supplier terms.</p>{% if can_join %}<a class="main-button" href="{{ url_for('vendor_profile') }}">Create or Edit Vendor Profile</a>{% elif session.get('user_id') %}<a class="main-button" href="{{ url_for('pricing') }}">Upgrade to Monthly or Yearly</a>{% else %}<a class="main-button" href="{{ url_for('register') }}">Create Account</a>{% endif %}</div></div>
    <div class="container"><div class="vendor-grid">{% for vendor in curated %}<article class="card vendor-card"><p class="tag">CURATED SOURCE</p><h2>{{ vendor.business_name }}</h2><p><strong>Service area:</strong> {{ vendor.location }}</p><p>{{ vendor.description }}</p><p><strong>Contact:</strong> {{ vendor.contact }}</p><a class="main-button" href="{{ vendor.website }}" target="_blank" rel="noopener noreferrer">Visit Vendor</a><a class="main-button hero-secondary" href="{{ url_for('submit_report',target_type='Vendor',target_name=vendor.business_name,target_url=vendor.website) }}">Report Vendor</a></article>{% endfor %}</div></div>
    <div class="container"><div class="vendor-grid">{% if vendors %}{% for vendor in vendors %}<article class="card vendor-card">{% if vendor.logo_filename %}<img class="vendor-logo" src="{{ url_for('vendor_logo', filename=vendor.logo_filename) }}" alt="{{ vendor.business_name }} logo">{% endif %}<p class="tag">COMMUNITY PROFILE</p><h2>{{ vendor.business_name }}</h2><p class="vendor-meta"><strong>Location:</strong> {{ vendor.location }}<br><strong>Contact:</strong> {{ vendor.contact }}</p><p>{{ vendor.description }}</p>{% if vendor.website %}<a class="main-button" href="{{ vendor.website }}" target="_blank" rel="noopener noreferrer">Visit Vendor</a>{% endif %}<a class="main-button hero-secondary" href="{{ url_for('submit_report',target_type='Vendor',target_name=vendor.business_name,target_url=(vendor.website or '')) }}">Report Vendor</a></article>{% endfor %}{% else %}<div class="card wide-card"><h2>No community vendors listed yet</h2><p>Community vendor profiles appear here after an active Monthly or Yearly member creates one.</p></div>{% endif %}</div></div>
    """
    return page("Vendors | FlipFinder", render_template_string(template, vendors=vendor_rows, curated=curated, can_join=_can_use_vendor_tools(user)))


@app.route("/vendor-profile", methods=["GET", "POST"])
@login_required
def vendor_profile():
    user = get_current_user()
    if not _can_use_vendor_tools(user):
        return redirect(url_for("pricing"))
    error = ""
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        existing = connection.execute("SELECT * FROM vendors WHERE user_id=?", (user["id"],)).fetchone()
    if request.method == "POST":
        business_name=request.form.get("business_name","").strip(); description=request.form.get("description","").strip(); location=request.form.get("location","").strip(); website=request.form.get("website","").strip(); contact=request.form.get("contact","").strip(); logo=request.files.get("logo")
        if not all([business_name,description,location,contact]): error="Business name, description, location and contact details are required."
        elif len(description)>1200: error="Vendor description must be 1,200 characters or fewer."
        else:
            logo_filename=existing["logo_filename"] if existing else None
            if logo and logo.filename:
                if not allowed_iphone_image(logo.filename): error="Logo must be PNG, JPG, JPEG or WEBP."
                else:
                    ext=secure_filename(logo.filename).rsplit(".",1)[1].lower(); logo_filename=f"vendor_{user['id']}_{secrets.token_hex(12)}.{ext}"; logo.save(os.path.join(app.config["UPLOAD_FOLDER"],logo_filename))
            if not error:
                with sqlite3.connect(DB_PATH) as connection:
                    connection.execute("""INSERT INTO vendors(user_id,business_name,description,location,website,contact,logo_filename) VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET business_name=excluded.business_name,description=excluded.description,location=excluded.location,website=excluded.website,contact=excluded.contact,logo_filename=excluded.logo_filename""",(user["id"],business_name,description,location,website or None,contact,logo_filename))
                return redirect(url_for("vendors"))
    template="""<div class="container"><div class="card wide-card"><p class="tag">MONTHLY + YEARLY MEMBER FEATURE</p><h1>Your Vendor Profile</h1>{% if error %}<p class="error">{{ error }}</p>{% endif %}<form method="post" enctype="multipart/form-data"><label>Business name</label><input name="business_name" value="{{ existing.business_name if existing else '' }}" required><label>Business description</label><textarea name="description" rows="7" maxlength="1200" required>{{ existing.description if existing else '' }}</textarea><label>Location or service area</label><input name="location" value="{{ existing.location if existing else '' }}" required><label>Website (optional)</label><input name="website" type="url" value="{{ existing.website if existing and existing.website else '' }}" placeholder="https://example.com"><label>Public contact details</label><input name="contact" value="{{ existing.contact if existing else '' }}" required><label>Business logo (optional)</label><input name="logo" type="file" accept="image/png,image/jpeg,image/webp"><button class="submit-button">Save Vendor Profile</button></form></div></div>"""
    return page("Vendor Profile | FlipFinder",render_template_string(template,existing=existing,error=error))


@app.route("/vendor-logo/<path:filename>")
def vendor_logo(filename):
    safe_name=secure_filename(filename)
    if safe_name != filename: return "Invalid filename",400
    return send_from_directory(app.config["UPLOAD_FOLDER"],safe_name)




@app.route("/report", methods=["GET", "POST"])
def submit_report():
    target_type = request.values.get("target_type", "Listing").strip()[:40]
    target_name = request.values.get("target_name", "").strip()[:200]
    target_url = request.values.get("target_url", "").strip()[:1000]
    message = ""
    if request.method == "POST":
        reason = request.form.get("reason", "").strip()[:120]
        details = request.form.get("details", "").strip()[:2000]
        if not target_name or not reason:
            message = "Enter a target name and reason."
        else:
            with sqlite3.connect(DB_PATH) as connection:
                connection.execute("INSERT INTO reports(user_id,target_type,target_name,target_url,reason,details) VALUES(?,?,?,?,?,?)", (session.get("user_id"),target_type,target_name,target_url or None,reason,details or None))
            message = "Report submitted for review."
    template="""<div class='container'><div class='card wide-card'><p class='tag'>REPORT SYSTEM</p><h1>Report a Vendor or Listing</h1>{% if message %}<p class='warning'>{{ message }}</p>{% endif %}<form method='post'><input type='hidden' name='target_type' value='{{ target_type }}'><input type='hidden' name='target_url' value='{{ target_url }}'><label>Type</label><input value='{{ target_type }}' disabled><label>Vendor or listing name</label><input name='target_name' value='{{ target_name }}' required><label>Reason</label><select name='reason' required><option value=''>Choose a reason</option><option>Suspected scam</option><option>Counterfeit or misleading item</option><option>Incorrect price or description</option><option>Broken or unsafe link</option><option>Spam or abusive content</option><option>Other</option></select><label>Extra details</label><textarea name='details' rows='6' maxlength='2000'></textarea><button class='submit-button'>Submit Report</button></form></div></div>"""
    return page("Report | FlipFinder",render_template_string(template,target_type=target_type,target_name=target_name,target_url=target_url,message=message))

@app.route("/reports")
@login_required
def reports():
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory=sqlite3.Row
        rows=connection.execute("SELECT * FROM reports WHERE user_id=? ORDER BY id DESC LIMIT 50",(session["user_id"],)).fetchall()
    template="""<div class='container'><div class='card wide-card'><p class='tag'>MY REPORTS</p><h1>Submitted Reports</h1>{% if not rows %}<p>You have not submitted any reports.</p>{% endif %}{% for r in rows %}<div class='history-item'><strong>{{ r.target_type }}: {{ r.target_name }}</strong><p>{{ r.reason }} · Status: {{ r.status }}</p><p>{{ r.details or '' }}</p><small>{{ r.created_at }}</small></div>{% endfor %}</div></div>"""
    return page("Reports | FlipFinder",render_template_string(template,rows=rows))

def run_price_alert(alert):
    matches=[]
    try:
        for item in search_ebay(alert["model"], max_price=str(alert["max_price"]), condition="USED"):
            try: price=float(item.get("price",{}).get("value",0))
            except (TypeError,ValueError): continue
            if price and price <= float(alert["max_price"]):
                matches.append({"title":item.get("title","iPhone listing"),"price":price,"url":item.get("itemWebUrl","")})
    except (requests.RequestException,RuntimeError):
        return []
    return matches[:5]

@app.route("/price-alerts", methods=["GET","POST"])
@login_required
def price_alerts():
    message=""
    if request.method=="POST":
        action=request.form.get("action","create")
        if action=="create":
            model=request.form.get("model","").strip()[:120]
            try: max_price=float(request.form.get("max_price",0))
            except ValueError: max_price=0
            if model and max_price>0:
                with sqlite3.connect(DB_PATH) as connection:
                    connection.execute("INSERT INTO price_alerts(user_id,model,max_price) VALUES(?,?,?)",(session["user_id"],model,max_price))
                message="Price alert created."
            else: message="Enter a model and valid maximum price."
        elif action=="check":
            alert_id=request.form.get("alert_id")
            with sqlite3.connect(DB_PATH) as connection:
                connection.row_factory=sqlite3.Row
                alert=connection.execute("SELECT * FROM price_alerts WHERE id=? AND user_id=?",(alert_id,session["user_id"])).fetchone()
                if alert:
                    matches=run_price_alert(alert)
                    for match in matches:
                        connection.execute("INSERT INTO notifications(user_id,title,message,url) VALUES(?,?,?,?)",(session["user_id"],f"Cheap {alert['model']} found",f"{match['title']} at A${match['price']:.2f}",match['url']))
                    connection.execute("UPDATE price_alerts SET last_checked_at=CURRENT_TIMESTAMP WHERE id=?",(alert_id,))
                    message=f"Check complete. {len(matches)} matching listing(s) added to notifications."
        elif action=="delete":
            with sqlite3.connect(DB_PATH) as connection:
                connection.execute("DELETE FROM price_alerts WHERE id=? AND user_id=?",(request.form.get("alert_id"),session["user_id"]))
            message="Price alert deleted."
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory=sqlite3.Row
        alerts=connection.execute("SELECT * FROM price_alerts WHERE user_id=? ORDER BY id DESC",(session["user_id"],)).fetchall()
        notes=connection.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 30",(session["user_id"],)).fetchall()
    template="""<div class='container'><div class='card'><p class='tag'>IPHONE PRICE ALERTS</p><h1>Find Cheap iPhones</h1>{% if message %}<p class='warning'>{{ message }}</p>{% endif %}<form method='post'><input type='hidden' name='action' value='create'><label>iPhone model</label><input name='model' placeholder='Example: iPhone 13 128GB' required><label>Maximum price (AUD)</label><input name='max_price' type='number' min='1' step='1' required><button class='submit-button'>Create Alert</button></form></div><div class='card'><h2>Your Alerts</h2>{% if not alerts %}<p>No alerts yet.</p>{% endif %}{% for a in alerts %}<div class='history-item'><strong>{{ a.model }}</strong><p>Maximum A${{ '%.2f'|format(a.max_price) }}{% if a.last_checked_at %} · Last checked {{ a.last_checked_at }}{% endif %}</p><form method='post' style='display:inline'><input type='hidden' name='action' value='check'><input type='hidden' name='alert_id' value='{{ a.id }}'><button class='main-button'>Check Now</button></form><form method='post' style='display:inline'><input type='hidden' name='action' value='delete'><input type='hidden' name='alert_id' value='{{ a.id }}'><button class='clear-button' style='width:auto'>Delete</button></form></div>{% endfor %}</div></div><div class='container'><div class='card wide-card'><h2>Notifications</h2>{% if not notes %}<p>No cheap-iPhone notifications yet.</p>{% endif %}{% for n in notes %}<div class='history-item'><strong>{{ n.title }}</strong><p>{{ n.message }}</p>{% if n.url %}<a class='main-button' href='{{ n.url }}' target='_blank' rel='noopener noreferrer'>View Listing</a>{% endif %}<small>{{ n.created_at }}</small></div>{% endfor %}</div></div>"""
    return page("iPhone Price Alerts | FlipFinder",render_template_string(template,alerts=alerts,notes=notes,message=message))

TEMU_CACHE = {}
TEMU_CACHE_SECONDS = 600

def search_temu_products(query, max_results=16):
    phone_query = f"Apple iPhone {query.strip()} unlocked smartphone"
    key = phone_query.casefold().strip()
    cached = TEMU_CACHE.get(key)
    if cached and time.time() - cached["saved_at"] < TEMU_CACHE_SECONDS:
        return cached["products"]
    token = (os.getenv("APIFY_API_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Missing APIFY_API_TOKEN in .env")
    response = requests.post(
        "https://api.apify.com/v2/acts/ExmLA8TXvdcFqsnnx/run-sync-get-dataset-items",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"searchTerms": [phone_query], "maxResultsPerTerm": 48},
        params={"format": "json", "clean": "true", "maxItems": 48, "maxTotalChargeUsd": 0.15, "timeout": 120},
        timeout=150,
    )
    response.raise_for_status()
    items = response.json()
    products=[]
    accessory_words = (
        "case", "cover", "protector", "tempered glass", "screen protector",
        "charger", "charging", "cable", "adapter", "holder", "mount",
        "stand", "strap", "skin", "sticker", "replacement", "repair",
        "battery case", "wallet case", "lens protector", "phone charm",
        "earbuds", "headphones", "smartwatch"
    )
    phone_words = ("iphone",)
    for item in items if isinstance(items,list) else []:
        title = str(item.get("title") or "").strip()
        title_lower = title.casefold()
        if not title or "iphone" not in title_lower:
            continue
        if any(brand in title_lower for brand in ("samsung", "galaxy", "google pixel", "xiaomi", "oppo", "motorola", "nokia", "huawei", "android")):
            continue
        if any(word in title_lower for word in accessory_words):
            continue
        url=item.get("url") or item.get("productUrl") or ""
        if not url: continue
        products.append({
            "title": title,
            "price": item.get("priceText") or item.get("price") or "Price unavailable",
            "market_price": item.get("originalPrice") or "",
            "sold": item.get("soldText") or item.get("soldCount") or "",
            "image": item.get("imageUrl") or "",
            "url": url,
        })
    products = products[:max_results]
    if products:
        TEMU_CACHE[key] = {"saved_at": time.time(), "products": products}
    return products

@app.route("/temu-search")
def temu_search():
    query=request.args.get("query","").strip(); cards=[]; error=""
    if query:
        try: cards=search_temu_products(query)
        except (requests.RequestException,RuntimeError,ValueError) as exc: error=str(exc)
    template="""
    <div class='container'><div class='card wide-card'><p class='tag'>TEMU IPHONE SEARCH</p><h1>Search Temu for iPhones</h1>
    <form method='get'><label>iPhone model</label><input name='query' value='{{ query }}' placeholder='Example: 13 Pro 128GB' required><button class='submit-button' id='temuSearchButton' onclick="this.disabled=true;this.textContent='Searching Temu...';this.form.submit();">Search Temu</button></form>
    <p class='warning'>Shows up to 16 iPhones only. Samsung, Android phones, cases, chargers, cables and other accessories are filtered out. Successful searches are cached for 10 minutes.</p></div></div>
    {% if error %}<div class='container'><div class='card'><p class='error'>{{ error }}</p></div></div>{% elif query and not cards %}<div class='container'><div class='card'><p>No actual iPhone listings were found. Other phone brands and accessories were removed.</p></div></div>{% endif %}
    <div class='container'>{% for item in cards %}<div class='card'>{% if item.image %}<img class='listing-image' src='{{ item.image }}'>{% endif %}<h3>{{ item.title }}</h3><p class='listing-price'>{{ item.price }}</p>{% if item.market_price %}<p>Original: {{ item.market_price }}</p>{% endif %}{% if item.sold %}<p>Sold: {{ item.sold }}</p>{% endif %}<a class='main-button' href='{{ item.url }}' target='_blank' rel='noopener noreferrer'>View on Temu</a></div>{% endfor %}</div>
    """
    return page("Temu Search | FlipFinder",render_template_string(template,query=query,cards=cards,error=error))

ALIBABA_CACHE = {}
ALIBABA_CACHE_SECONDS = 300

def search_alibaba_iphones(query, max_results=24):
    search_query = f"original used refurbished Apple iPhone {query.strip()} unlocked mobile phone handset wholesale"
    key = search_query.casefold()
    cached = ALIBABA_CACHE.get(key)
    if cached and time.time() - cached["saved_at"] < ALIBABA_CACHE_SECONDS:
        return cached["products"]
    token = (os.getenv("APIFY_API_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Missing APIFY_API_TOKEN in .env")
    response = requests.post(
        "https://api.apify.com/v2/actors/xtracto~alibaba-search-scraper/run-sync-get-dataset-items",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "queries": [search_query],
            "maxPagesPerQuery": 2,
            "maxConcurrency": 2,
            "maxRequestRetries": 3,
            "proxyConfiguration": {"useApifyProxy": True},
        },
        params={"format": "json", "clean": "true", "maxItems": 80, "timeout": 180},
        timeout=210,
    )
    if not response.ok:
        try:
            detail = response.json().get("error", {}).get("message") or response.text
        except ValueError:
            detail = response.text
        raise RuntimeError(f"Alibaba Actor error {response.status_code}: {detail[:500]}")
    # Alibaba often returns accessories containing the word "iPhone". Require phone-like
    # wording and reject accessory, replacement-part, packaging and service listings.
    blocked = (
        "case", "cases", "cover", "covers", "protector", "protective", "tempered glass",
        "screen guard", "charger", "charging", "cable", "adapter", "holder", "mount",
        "stand", "strap", "skin", "sticker", "shell", "bumper", "wallet", "pouch",
        "bag", "sleeve", "phone charm", "earbuds", "headphones", "smartwatch",
        "replacement", "repair", "spare part", "parts only", "for parts", "lcd",
        "oled screen", "display screen", "touch screen", "digitizer", "housing",
        "back glass", "camera lens", "battery replacement", "battery case", "flex cable",
        "logic board", "motherboard", "empty box", "box only", "packaging", "dummy",
        "mockup", "display model", "phone accessory", "mobile accessory"
    )
    other_brands = (
        "samsung", "galaxy", "google pixel", "xiaomi", "oppo", "motorola", "nokia",
        "huawei", "android", "oneplus", "vivo", "realme", "honor"
    )
    phone_signals = (
        "unlocked", "smartphone", "mobile phone", "cell phone", "handset", "refurbished",
        "used phone", "original phone", "face id", "ios", "imei", "esim", "dual sim",
        "64gb", "128gb", "256gb", "512gb", "1tb"
    )
    model_pattern = re.compile(r"\biphone\s*(?:se|x[rs]?|1[1-9])(?:\s*(?:pro|max|plus|mini|air))*\b", re.I)
    products = []
    seen_urls = set()
    for item in response.json():
        title = str(item.get("title") or "").strip()
        low = re.sub(r"\s+", " ", title.casefold())
        description = str(item.get("description") or item.get("shortDescription") or "").casefold()
        category = str(item.get("categoryName") or item.get("category") or "").casefold()
        combined = f"{low} {description} {category}"

        if not title or "iphone" not in low:
            continue
        if any(term in combined for term in blocked) or any(brand in combined for brand in other_brands):
            continue
        if not model_pattern.search(low):
            continue
        if not any(signal in combined for signal in phone_signals):
            continue

        # Accessory listings are often extremely cheap. If a numeric price is available,
        # reject a maximum listed price below A$35 equivalent rather than showing a cover.
        price_text = str(item.get("priceFormatted") or item.get("price") or "")
        price_numbers = []
        for number in re.findall(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", price_text):
            try:
                price_numbers.append(float(number.replace(",", "")))
            except ValueError:
                pass
        if price_numbers and max(price_numbers) < 35:
            continue

        url = item.get("url") or ""
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        moq = " ".join(str(x) for x in (item.get("minOrderQuantity"), item.get("minOrderUnit")) if x)
        products.append({
            "title": title,
            "price": item.get("priceFormatted") or "Price unavailable",
            "brand": "Apple",
            "supplier": item.get("companyName") or "",
            "moq": moq,
            "country": item.get("countryCode") or "",
            "gold_years": item.get("goldSupplierYears") or "",
            "trade_assurance": item.get("tradeAssurance"),
            "image": item.get("mainImage") or "",
            "url": url,
        })
    products = products[:max_results]
    if products:
        ALIBABA_CACHE[key] = {"saved_at": time.time(), "products": products}
    return products

@app.route("/alibaba-search")
def alibaba_search():
    query = request.args.get("query", "").strip()
    cards, error = [], ""
    if query:
        try:
            cards = search_alibaba_iphones(query)
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            error = str(exc)
    template = """
    <div class='container'><div class='card wide-card'><p class='tag'>ALIBABA IPHONE SEARCH</p><h1>Search Alibaba for Wholesale iPhones</h1>
    <form method='get'><label>iPhone model</label><input name='query' value='{{ query }}' placeholder='Example: 13 Pro 128GB' required><button class='submit-button' onclick="this.disabled=true;this.textContent='Searching Alibaba...';this.form.submit();">Search Alibaba</button></form>
    <p class='warning'>Shows up to 24 iPhone listings across two Alibaba search pages. Accessories and other phone brands are filtered out. Check MOQ, supplier history, shipping, warranty and landed cost before ordering.</p></div></div>
    {% if error %}<div class='container'><div class='card'><p class='error'>{{ error }}</p></div></div>{% elif query and not cards %}<div class='container'><div class='card'><p>No actual iPhone listings were found. Other brands and accessories were removed.</p></div></div>{% endif %}
    <div class='container'>{% for item in cards %}<div class='card'>{% if item.image %}<img class='listing-image' src='{{ item.image }}' alt='Alibaba iPhone listing image'>{% endif %}<h3>{{ item.title }}</h3><p class='listing-price'>{{ item.price }}</p>{% if item.supplier %}<p><strong>Supplier:</strong> {{ item.supplier }}</p>{% endif %}{% if item.moq %}<p><strong>MOQ:</strong> {{ item.moq }}</p>{% endif %}{% if item.country %}<p><strong>Country:</strong> {{ item.country }}</p>{% endif %}{% if item.gold_years %}<p><strong>Gold supplier:</strong> {{ item.gold_years }}</p>{% endif %}{% if item.trade_assurance is not none %}<p><strong>Trade Assurance:</strong> {{ 'Yes' if item.trade_assurance else 'No' }}</p>{% endif %}<a class='main-button' href='{{ item.url }}' target='_blank' rel='noopener noreferrer'>View on Alibaba</a></div>{% endfor %}</div>
    """
    return page("Alibaba iPhone Search | FlipFinder", render_template_string(template, query=query, cards=cards, error=error))

@app.route("/market-values")
def market_values():
    models = ["iPhone 11 128GB", "iPhone 12 128GB", "iPhone 13 128GB", "iPhone 14 128GB", "iPhone 15 128GB", "iPhone 16 128GB"]
    market_data, error_message = [], ""
    try:
        for model in models:
            prices = []
            for item in search_ebay(model, condition="USED"):
                try:
                    price = float(item.get("price", {}).get("value", 0))
                except (TypeError, ValueError):
                    continue
                if price > 50:
                    prices.append(price)
            if prices:
                market_data.append({"model": model, "median": round(statistics.median(prices), 2), "lowest": round(min(prices), 2), "highest": round(max(prices), 2), "count": len(prices)})
    except (requests.RequestException, RuntimeError) as error:
        error_message = str(error)

    checked_at = datetime.now().strftime("%d %b %Y, %I:%M %p")
    labels = [x["model"] for x in market_data]
    medians = [x["median"] for x in market_data]
    lows = [x["lowest"] for x in market_data]
    highs = [x["highest"] for x in market_data]
    template = """
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <div class="container"><div class="card wide-card"><p class="tag">LIVE APPLE ANALYSIS</p><h1>Live iPhone Market Chart</h1><p>Live used-iPhone asking prices from eBay Australia.</p><p><strong>Last refreshed:</strong> {{ checked_at }}</p>
    {% if error_message %}<p class="error">{{ error_message }}</p>{% endif %}
    {% if market_data %}<div class="chart-container"><canvas id="appleMarketChart"></canvas></div><div class="features">{% for item in market_data %}<div class="card feature-card"><h3>{{ item.model }}</h3><p class="listing-price">Median A${{ '%.2f'|format(item.median) }}</p><p>Lowest: A${{ '%.2f'|format(item.lowest) }}</p><p>Highest: A${{ '%.2f'|format(item.highest) }}</p><p>{{ item.count }} listings</p></div>{% endfor %}</div>
    <script>new Chart(document.getElementById('appleMarketChart'),{type:'line',data:{labels:{{ labels|tojson }},datasets:[{label:'Median',data:{{ medians|tojson }},borderColor:'#69a3ff',backgroundColor:'rgba(105,163,255,.2)',borderWidth:3,tension:.28,fill:true},{label:'Lowest',data:{{ lows|tojson }},borderColor:'#5ee59d',borderWidth:2,tension:.28},{label:'Highest',data:{{ highs|tojson }},borderColor:'#ffbe55',borderWidth:2,tension:.28}]},options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},plugins:{legend:{labels:{color:'#fff'}}},scales:{x:{ticks:{color:'#cbd4e8'},grid:{color:'rgba(255,255,255,.06)'}},y:{ticks:{color:'#cbd4e8',callback:(v)=>'A$'+v},grid:{color:'rgba(255,255,255,.08)'}}}}});</script>
    {% elif not error_message %}<p>No usable live market data was returned.</p>{% endif %}<p class="warning">Current asking prices, not confirmed sold prices.</p><a class="main-button" href="{{ url_for('market_values') }}">Refresh Live Analysis</a><a class="main-button" href="{{ url_for('iphone_community') }}">View Community iPhones</a></div></div>
    """
    return page("Live Apple Analysis | FlipFinder", render_template_string(template, market_data=market_data, checked_at=checked_at, error_message=error_message, labels=labels, medians=medians, lows=lows, highs=highs))


@app.route("/clear-history", methods=["POST"])
@login_required
def clear_history():
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("DELETE FROM deals WHERE user_id = ?", (session["user_id"],))
    return redirect(url_for("analyse"))


@app.route("/analyse", methods=["GET", "POST"])
@login_required
def analyse():
    result_html = ""
    if request.method == "POST":
        phone = request.form.get("phone", "")
        condition = request.form.get("condition", "Excellent")
        description = request.form.get("description", "").lower()
        try:
            price = float(request.form.get("price") or 0)
            battery = int(request.form.get("battery") or 100)
            fees = float(request.form.get("fees") or 0)
            shipping = float(request.form.get("shipping") or 0)
        except ValueError:
            return page("Input Error", "<div class='container'><div class='card'><p>Please enter valid numbers.</p></div></div>")

        value = PHONE_VALUES.get(phone)
        if value is None:
            return page("Input Error", "<div class='container'><div class='card'><p>Phone model not found.</p></div></div>")
        if battery < 80: value -= 100
        elif battery < 85: value -= 70
        elif battery < 90: value -= 40
        elif battery < 95: value -= 20
        value += {"Excellent":0,"Good":-30,"Fair":-70,"Poor":-150}.get(condition,0)
        total_cost = price + fees + shipping
        profit = value - total_cost
        if profit >= 150: rating, score = "GREAT DEAL",95
        elif profit >= 100: rating, score = "BUY",80
        elif profit >= 50: rating, score = "MAYBE",60
        else: rating, score = "SKIP",30
        warnings = [p for p in ["can't test","icloud locked","need gone today","no returns","parts only","forgot password"] if p in description]
        risk = "High risk" if warnings else "No obvious warnings"
        warning_text = ", ".join(warnings) if warnings else "None"
        save_deal(phone, profit, rating)
        result_html = render_template_string("""<div class="card"><p class="tag">ANALYSIS COMPLETE</p><h2>{{ rating }}</h2><p><strong>Phone:</strong> {{ phone }}</p><p><strong>Estimated value:</strong> ${{ '%.2f'|format(value) }}</p><p><strong>Total cost:</strong> ${{ '%.2f'|format(total_cost) }}</p><p class="profit">Estimated profit: ${{ '%.2f'|format(profit) }}</p><p><strong>Deal score:</strong> {{ score }}/100</p><p><strong>Risk:</strong> {{ risk }}</p><p class="warning"><strong>Warnings:</strong> {{ warning_text }}</p></div>""", rating=rating, phone=phone, value=value, total_cost=total_cost, profit=profit, score=score, risk=risk, warning_text=warning_text)

    deals = load_deals()
    total_deals, average_profit, best_profit = load_statistics()
    template = """
    <div class="features"><div class="card feature-card"><h2>{{ total_deals }}</h2><p>Total Deals</p></div><div class="card feature-card"><h2>${{ '%.2f'|format(average_profit) }}</h2><p>Average Profit</p></div><div class="card feature-card"><h2>${{ '%.2f'|format(best_profit) }}</h2><p>Best Profit</p></div></div>
    <div class="container"><div class="card"><p class="tag">DEAL CALCULATOR</p><h1>Analyse a Deal</h1><form method="post"><label>Phone</label><select name="phone">{% for name in phone_names %}<option>{{ name }}</option>{% endfor %}</select><label>Buying price</label><input type="number" step="0.01" name="price" required><label>Battery health</label><input type="number" name="battery" min="1" max="100" value="100" required><label>Condition</label><select name="condition"><option>Excellent</option><option>Good</option><option>Fair</option><option>Poor</option></select><label>Selling fees</label><input type="number" step="0.01" name="fees" value="0"><label>Shipping cost</label><input type="number" step="0.01" name="shipping" value="0"><label>Listing description</label><textarea name="description" rows="5"></textarea><button class="submit-button">Analyse Deal</button></form></div>{{ result_html|safe }}<div class="card"><p class="tag">LATEST ACTIVITY</p><h2>Recent Deals</h2>{% if deals %}{% for phone,profit,rating in deals %}<div class="history-item"><strong>{{ phone }}</strong><p>{{ rating }}</p><span class="history-profit">Profit: ${{ '%.2f'|format(profit) }}</span></div>{% endfor %}{% else %}<p>No deals analysed yet.</p>{% endif %}<form method="post" action="/clear-history" onsubmit="return confirm('Clear all your saved deals?')"><button class="clear-button" type="submit">Clear History</button></form></div></div>
    """
    body = render_template_string(template, total_deals=total_deals, average_profit=average_profit, best_profit=best_profit, phone_names=PHONE_VALUES.keys(), deals=deals, result_html=result_html)
    return page("Analyse Deal | FlipFinder", body)


setup_database()
setup_password_reset_table()

if __name__ == "__main__":
    app.run(debug=True, use_reloader=False) 