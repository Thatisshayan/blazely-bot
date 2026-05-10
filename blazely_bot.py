# blazely_bot_v3.py
# Blazely Bot V3 — Full System Upgrade
# Built: May 2026

import logging
import sqlite3
import os
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)
import aiohttp

load_dotenv()

# ── ENV CONFIG ────────────────────────────────────────────────
BOT_TOKEN       = os.getenv("BOT_TOKEN")
ADMIN_ID        = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")
WEBHOOK_SECRET  = os.getenv("WEBHOOK_SECRET", "blazely2026")
PORT            = int(os.getenv("PORT", "8443"))
DOMAIN          = os.getenv("RAILWAY_DOMAIN", "")
DB_PATH         = os.getenv("DB_PATH", "blazely_crm.db")

# ── TIERED COMMISSION RATES ───────────────────────────────────
COMMISSION_RATES = {
    "referral_only":      0.10,
    "lead_conversation":  0.15,
    "rep_closes":         0.20,
    "company_lead_low":   0.10,
    "company_lead_high":  0.15,
}
BONUS_RATES = {
    5:  0.225,
    10: 0.25,
}

# ── DEAL PIPELINE STAGES ─────────────────────────────────────
PIPELINE_STAGES = [
    "new_lead",
    "contacted",
    "proposal_sent",
    "negotiating",
    "deal_closed",
    "payment_verified",
    "admin_approved",
    "commission_approved",
    "paid",
    "lost",
]

# ── CONVERSATION STATES ───────────────────────────────────────
(
    NL_BUSINESS, NL_CONTACT, NL_SERVICE, NL_PRICE,
    NL_DEAL_TYPE, NL_LEAD_SOURCE,
    UPD_SELECT, UPD_STATUS, UPD_NOTES,
    APPROVE_SELECT, APPROVE_ACTION,
    DEMO_BUSINESS,
) = range(12)

# ── LOGGING ───────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("BlazelyBot")


# ════════════════════════════════════════════════════════════════
# DATABASE
# ════════════════════════════════════════════════════════════════

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS reps (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id     INTEGER UNIQUE NOT NULL,
            username        TEXT,
            full_name       TEXT,
            phone           TEXT,
            joined_at       TEXT DEFAULT (datetime('now')),
            is_active       INTEGER DEFAULT 1,
            total_earned    REAL DEFAULT 0.0,
            wallet_pending  REAL DEFAULT 0.0,
            wallet_approved REAL DEFAULT 0.0,
            wallet_paid     REAL DEFAULT 0.0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            rep_id            INTEGER NOT NULL,
            business_name     TEXT NOT NULL,
            contact_name      TEXT,
            contact_phone     TEXT,
            contact_email     TEXT,
            service_interest  TEXT,
            deal_value        REAL DEFAULT 0.0,
            deal_type         TEXT DEFAULT 'lead_conversation',
            lead_source       TEXT DEFAULT 'rep',
            status            TEXT DEFAULT 'new_lead',
            notes             TEXT,
            commission_rate   REAL DEFAULT 0.15,
            commission_amount REAL DEFAULT 0.0,
            commission_status TEXT DEFAULT 'pending',
            logged_at         TEXT DEFAULT (datetime('now')),
            updated_at        TEXT DEFAULT (datetime('now')),
            last_nudge_at     TEXT,
            demo_requested    INTEGER DEFAULT 0,
            FOREIGN KEY (rep_id) REFERENCES reps(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS admin_approvals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id     INTEGER NOT NULL,
            rep_id      INTEGER NOT NULL,
            action      TEXT NOT NULL,
            note        TEXT,
            approved_at TEXT DEFAULT (datetime('now'))
        )
    """)

    conn.commit()
    conn.close()
    log.info("✅ Database initialized")


# ════════════════════════════════════════════════════════════════
# REP FUNCTIONS
# ════════════════════════════════════════════════════════════════

def get_rep(telegram_id: int):
    conn = get_db()
    rep = conn.execute(
        "SELECT * FROM reps WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    conn.close()
    return rep


def register_rep(telegram_id: int, username: str, full_name: str, phone: str):
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO reps (telegram_id, username, full_name, phone)
           VALUES (?, ?, ?, ?)""",
        (telegram_id, username, full_name, phone)
    )
    conn.commit()
    conn.close()


def get_all_reps():
    conn = get_db()
    reps = conn.execute(
        "SELECT * FROM reps WHERE is_active = 1 ORDER BY total_earned DESC"
    ).fetchall()
    conn.close()
    return reps


def update_rep_wallet(rep_id: int, pending_delta=0, approved_delta=0, paid_delta=0):
    conn = get_db()
    conn.execute("""
        UPDATE reps SET
            wallet_pending  = wallet_pending  + ?,
            wallet_approved = wallet_approved + ?,
            wallet_paid     = wallet_paid     + ?,
            total_earned    = total_earned    + ?
        WHERE id = ?
    """, (pending_delta, approved_delta, paid_delta, max(paid_delta, 0), rep_id))
    conn.commit()
    conn.close()


# ════════════════════════════════════════════════════════════════
# LEAD FUNCTIONS
# ════════════════════════════════════════════════════════════════

def add_lead(rep_id, business_name, contact_name, contact_phone,
             contact_email, service_interest, deal_value,
             deal_type, lead_source):
    rate = calculate_commission_rate(deal_type, lead_source, rep_id)
    amount = round(deal_value * rate, 2)
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO leads
            (rep_id, business_name, contact_name, contact_phone,
             contact_email, service_interest, deal_value,
             deal_type, lead_source, commission_rate, commission_amount)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (rep_id, business_name, contact_name, contact_phone,
          contact_email, service_interest, deal_value,
          deal_type, lead_source, rate, amount))
    lead_id = c.lastrowid
    conn.commit()
    conn.close()
    return lead_id, rate, amount
def check_duplicate(business_name: str, contact_email: str = None):
    conn = get_db()
    dup = conn.execute("SELECT * FROM leads WHERE business_name = ?", (business_name,)).fetchone()
    if not dup and contact_email:
        dup = conn.execute("SELECT * FROM leads WHERE contact_email = ?", (contact_email,)).fetchone()
    conn.close()
    return dup



def get_lead(lead_id: int):
    conn = get_db()
    lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    conn.close()
    return lead


def get_rep_leads(rep_id: int):
    conn = get_db()
    leads = conn.execute(
        "SELECT * FROM leads WHERE rep_id = ? ORDER BY logged_at DESC",
        (rep_id,)
    ).fetchall()
    conn.close()
    return leads


def update_lead_status(lead_id: int, status: str, notes: str = None):
    conn = get_db()
    if notes:
        conn.execute("""
            UPDATE leads SET status = ?, notes = ?, updated_at = datetime('now')
            WHERE id = ?
        """, (status, notes, lead_id))
    else:
        conn.execute("""
            UPDATE leads SET status = ?, updated_at = datetime('now')
            WHERE id = ?
        """, (status, lead_id))
    conn.commit()
    conn.close()


def get_stale_leads(days: int = 5):
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    conn = get_db()
    leads = conn.execute("""
        SELECT l.*, r.full_name, r.telegram_id FROM leads l
        JOIN reps r ON l.rep_id = r.id
        WHERE l.updated_at < ?
        AND l.status NOT IN ('deal_closed','payment_verified',
                             'admin_approved','commission_approved','paid','lost')
        AND (l.last_nudge_at IS NULL OR l.last_nudge_at < ?)
    """, (cutoff, cutoff)).fetchall()
    conn.close()
    return leads


def get_pending_approvals():
    conn = get_db()
    leads = conn.execute("""
        SELECT l.*, r.full_name, r.telegram_id FROM leads l
        JOIN reps r ON l.rep_id = r.id
        WHERE l.status = 'deal_closed'
        AND l.commission_status = 'pending'
    """).fetchall()
    conn.close()
    return leads


# ════════════════════════════════════════════════════════════════
# COMMISSION LOGIC
# ════════════════════════════════════════════════════════════════

def calculate_commission_rate(deal_type: str, lead_source: str, rep_id: int) -> float:
    if lead_source == "company":
        base = COMMISSION_RATES["company_lead_high"] if deal_type == "rep_closes" \
               else COMMISSION_RATES["company_lead_low"]
    else:
        base = COMMISSION_RATES.get(deal_type, COMMISSION_RATES["lead_conversation"])

    conn = get_db()
    rep = conn.execute("SELECT id FROM reps WHERE telegram_id = ?", (rep_id,)).fetchone()
    if not rep:
        conn.close()
        return base
    month_start = datetime.now().replace(day=1).isoformat()
    closed_count = conn.execute("""
        SELECT COUNT(*) FROM leads
        WHERE rep_id = ? AND status IN
        ('deal_closed','payment_verified','admin_approved','commission_approved','paid')
        AND updated_at >= ?
    """, (rep["id"], month_start)).fetchone()[0]
    conn.close()

    for threshold in sorted(BONUS_RATES.keys(), reverse=True):
        if closed_count >= threshold:
            return max(base, BONUS_RATES[threshold])
    return base


def get_rep_monthly_stats(rep_id: int):
    month_start = datetime.now().replace(day=1).isoformat()
    conn = get_db()
    stats = conn.execute("""
        SELECT
            COUNT(*) as total_leads,
            SUM(CASE WHEN status NOT IN ('lost') THEN 1 ELSE 0 END) as active_leads,
            SUM(CASE WHEN status IN ('deal_closed','payment_verified',
                'admin_approved','commission_approved','paid') THEN 1 ELSE 0 END) as closed_deals,
            SUM(CASE WHEN commission_status = 'paid' THEN commission_amount ELSE 0 END) as earned_this_month
        FROM leads WHERE rep_id = ? AND logged_at >= ?
    """, (rep_id, month_start)).fetchone()
    conn.close()
    return stats


# ════════════════════════════════════════════════════════════════
# WEBHOOK
# ════════════════════════════════════════════════════════════════

async def fire_webhook(event: str, data: dict):
    if not N8N_WEBHOOK_URL:
        return
    try:
        payload = {"event": event, "timestamp": datetime.now().isoformat(), **data}
        async with aiohttp.ClientSession() as session:
            async with session.post(N8N_WEBHOOK_URL, json=payload, timeout=10) as resp:
                log.info(f"Webhook {event} → {resp.status}")
    except Exception as e:
        log.warning(f"Webhook failed: {e}")


# ════════════════════════════════════════════════════════════════
# COMMANDS — /start /help
# ════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rep = get_rep(user.id)

    if rep:
        await update.message.reply_text(
            f"🔥 *Welcome back, {rep['full_name']}!*\n\n"
            f"*Quick Commands:*\n"
            f"├ /newlead — Log a new lead\n"
            f"├ /myleads — View your pipeline\n"
            f"├ /wallet — Your earnings\n"
            f"├ /scripts — Sales scripts\n"
            f"├ /stats — Performance dashboard\n"
            f"├ /demo — Request a demo page\n"
            f"├ /remind — Stale leads check\n"
            f"└ /leaderboard — Top reps this month",
            parse_mode="Markdown"
        )
    else:
        keyboard = [[InlineKeyboardButton("✅ Register as Rep", callback_data="register")]]
        await update.message.reply_text(
            "🔥 *Welcome to Blazely!*\n\n"
            "Blaze your business online — and get paid for it.\n\n"
            "You're not registered yet. Hit the button below to join as a sales rep.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 *Blazely Bot V3 — Command Reference*\n\n"
        "*REP COMMANDS:*\n"
        "├ /newlead — Log a new prospect\n"
        "├ /myleads — View & manage your leads\n"
        "├ /update — Update a lead status\n"
        "├ /wallet — Earnings, pending & paid commissions\n"
        "├ /scripts — Sales pitch, objection handling, DM scripts\n"
        "├ /stats — Your monthly performance dashboard\n"
        "├ /demo — Request a demo preview for a prospect\n"
        "├ /remind — View stale leads (5+ days no update)\n"
        "└ /leaderboard — Top reps this month\n\n"
        "*ADMIN COMMANDS:*\n"
        "├ /approve — Approve deals & commissions\n"
        "├ /allleads — All leads across all reps\n"
        "└ /broadcast — Message all active reps\n\n"
        "*GOLDEN RULE:*\n"
        "_If it's not logged in the bot, it doesn't exist._",
        parse_mode="Markdown"
    )


# ════════════════════════════════════════════════════════════════
# REGISTRATION
# ════════════════════════════════════════════════════════════════

async def cb_register(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "📝 *Let's get you registered!*\n\nReply with your *full name*:",
        parse_mode="Markdown"
    )
    ctx.user_data["reg_step"] = "name"


async def handle_registration(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    step = ctx.user_data.get("reg_step")
    if not step:
        return
    text = update.message.text.strip()
    user = update.effective_user

    if step == "name":
        ctx.user_data["reg_name"] = text
        ctx.user_data["reg_step"] = "phone"
        await update.message.reply_text(
            f"✅ Got it, *{text}*!\n\nNow send your *phone number*:",
            parse_mode="Markdown"
        )
    elif step == "phone":
        register_rep(user.id, user.username or "", ctx.user_data["reg_name"], text)
        name = ctx.user_data["reg_name"]
        ctx.user_data.clear()
        await update.message.reply_text(
            f"🎉 *You're in!* Welcome to Blazely.\n\n"
            f"You're now registered as a sales rep.\n"
            f"Start logging leads with /newlead 🔥",
            parse_mode="Markdown"
        )
        if ADMIN_ID:
            await ctx.bot.send_message(
                ADMIN_ID,
                f"🆕 *New Rep Registered!*\n"
                f"Name: {name}\n"
                f"TG: @{user.username}\n"
                f"Phone: {text}",
                parse_mode="Markdown"
            )


# ════════════════════════════════════════════════════════════════
# /newlead CONVERSATION
# ════════════════════════════════════════════════════════════════

async def cmd_newlead(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rep = get_rep(user.id)
    if not rep:
        await update.message.reply_text("❌ Register first with /start")
        return ConversationHandler.END
    ctx.user_data["nl_rep_id"] = rep["id"]
    await update.message.reply_text(
    dup = check_duplicate(ctx.user_data["nl_business"])
    if dup:
        await update.message.reply_text(f"⚠️ *Duplicate Detected!*\n\nBusiness *{ctx.user_data["nl_business"]}* is already in the system (Lead #{dup["id"]}).\n\nIf you believe this is an error, contact an admin. Registration cancelled.", parse_mode="Markdown")
        return ConversationHandler.END

        "🏢 *New Lead — Step 1/6*\n\nWhat's the *business name*?",
        parse_mode="Markdown"
    )
    return NL_BUSINESS


async def nl_business(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["nl_business"] = update.message.text.strip()
    await update.message.reply_text(
        "👤 *Step 2/6 — Contact Info*\n\n"
        "Send: `Name, Phone, Email` (comma separated)\n"
        "_Example: John Doe, 416-555-0100, john@email.com_",
        parse_mode="Markdown"
    )
    return NL_CONTACT


async def nl_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = [p.strip() for p in update.message.text.split(",")]
    ctx.user_data["nl_contact_name"]  = parts[0] if len(parts) > 0 else ""
    ctx.user_data["nl_contact_phone"] = parts[1] if len(parts) > 1 else ""
    ctx.user_data["nl_contact_email"] = parts[2] if len(parts) > 2 else ""
    await update.message.reply_text(
        "💼 *Step 3/6 — Service Interest*\n\nWhat service are they interested in?\n"
        "_e.g. Landing Page, Full Website, Rebrand_",
        parse_mode="Markdown"
    )
    return NL_SERVICE


async def nl_service(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["nl_service"] = update.message.text.strip()
    await update.message.reply_text(
        "💰 *Step 4/6 — Deal Value*\n\nEstimated deal value? (numbers only)\n"
        "_e.g. 499_",
        parse_mode="Markdown"
    )
    return NL_PRICE


async def nl_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ctx.user_data["nl_price"] = float(update.message.text.strip().replace("$", ""))
    except ValueError:
        await update.message.reply_text("❌ Numbers only please. Try again:")
        return NL_PRICE

    keyboard = [
        [InlineKeyboardButton("📣 Referral Only (10%)", callback_data="dt_referral_only")],
        [InlineKeyboardButton("💬 Lead + Conversation (15%)", callback_data="dt_lead_conversation")],
        [InlineKeyboardButton("🤝 Rep Closes Deal (20%)", callback_data="dt_rep_closes")],
    ]
    await update.message.reply_text(
        "🎯 *Step 5/6 — Deal Type*\n\nHow involved are you in this deal?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return NL_DEAL_TYPE


async def nl_deal_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    deal_map = {
        "dt_referral_only":     "referral_only",
        "dt_lead_conversation": "lead_conversation",
        "dt_rep_closes":        "rep_closes",
    }
    ctx.user_data["nl_deal_type"] = deal_map[query.data]
    keyboard = [
        [InlineKeyboardButton("🙋 Self-sourced (I found them)", callback_data="src_rep")],
        [InlineKeyboardButton("🏢 Company-provided lead", callback_data="src_company")],
    ]
    await query.message.reply_text(
        "📍 *Step 6/6 — Lead Source*\n\nWhere did this lead come from?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return NL_LEAD_SOURCE


async def nl_lead_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    source = "rep" if query.data == "src_rep" else "company"
    d = ctx.user_data

    lead_id, rate, commission = add_lead(
        rep_id=d["nl_rep_id"],
        business_name=d["nl_business"],
        contact_name=d["nl_contact_name"],
        contact_phone=d["nl_contact_phone"],
        contact_email=d["nl_contact_email"],
        service_interest=d["nl_service"],
        deal_value=d["nl_price"],
        deal_type=d["nl_deal_type"],
        lead_source=source,
    )
    update_rep_wallet(d["nl_rep_id"], pending_delta=commission)

    await query.message.reply_text(
        f"✅ *Lead Logged! #{lead_id}*\n\n"
        f"🏢 {d['nl_business']}\n"
        f"💼 {d['nl_service']}\n"
        f"💰 Deal Value: ${d['nl_price']:,.2f}\n"
        f"🎯 Deal Type: {d['nl_deal_type'].replace('_', ' ').title()}\n"
        f"📍 Source: {'Self-sourced' if source == 'rep' else 'Company Lead'}\n"
        f"💸 Est. Commission: ${commission:,.2f} ({int(rate*100)}%)\n\n"
        f"_Commission releases after: payment verified → admin approved._",
        parse_mode="Markdown"
    )
    await fire_webhook("new_lead", {
        "lead_id": lead_id, "business": d["nl_business"],
        "rep_id": d["nl_rep_id"], "deal_value": d["nl_price"],
        "commission": commission, "rate": rate,
    })
    ctx.user_data.clear()
    return ConversationHandler.END


async def nl_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ════════════════════════════════════════════════════════════════
# /myleads
# ════════════════════════════════════════════════════════════════

async def cmd_myleads(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rep = get_rep(user.id)
    if not rep:
        await update.message.reply_text("❌ Register first with /start")
        return

    leads = get_rep_leads(rep["id"])
    if not leads:
        await update.message.reply_text("📭 No leads yet! Use /newlead to log your first prospect. 🔥")
        return

    active, closed, lost = [], [], []
    for l in leads:
        if l["status"] == "lost":
            lost.append(l)
        elif l["status"] in ("paid", "commission_approved", "admin_approved", "payment_verified"):
            closed.append(l)
        else:
            active.append(l)

    STATUS_EMOJI = {
        "new_lead": "🆕", "contacted": "📞", "proposal_sent": "📄",
        "negotiating": "🤝", "deal_closed": "🔒", "payment_verified": "💳",
        "admin_approved": "✅", "commission_approved": "💸", "paid": "🎉", "lost": "❌"
    }

    msg = f"📋 *Your Leads — {len(leads)} Total*\n\n"
    if active:
        msg += f"*🔥 Active ({len(active)}):*\n"
        for l in active[:10]:
            emoji = STATUS_EMOJI.get(l["status"], "•")
            msg += (f"{emoji} *#{l['id']} {l['business_name']}*\n"
                    f"   Status: {l['status'].replace('_', ' ').title()}\n"
                    f"   💰 ${l['deal_value']:,.0f} → Est. ${l['commission_amount']:,.2f} commission\n\n")
    if closed:
        msg += f"*✅ Closed ({len(closed)}):*\n"
        for l in closed[:5]:
            emoji = STATUS_EMOJI.get(l["status"], "✅")
            msg += f"{emoji} #{l['id']} {l['business_name']} — ${l['commission_amount']:,.2f}\n"
        msg += "\n"
    if lost:
        msg += f"*❌ Lost:* {len(lost)} leads\n\n"

    msg += "_Use /update to move leads forward._"
    await update.message.reply_text(msg, parse_mode="Markdown")


# ════════════════════════════════════════════════════════════════
# /update CONVERSATION
# ════════════════════════════════════════════════════════════════

async def cmd_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rep = get_rep(user.id)
    if not rep:
        await update.message.reply_text("❌ Register first with /start")
        return ConversationHandler.END

    leads = get_rep_leads(rep["id"])
    active = [l for l in leads if l["status"] not in ("paid", "lost")]
    if not active:
        await update.message.reply_text("No active leads. Use /newlead!")
        return ConversationHandler.END

    ctx.user_data["upd_rep_id"] = rep["id"]
    keyboard = [
        [InlineKeyboardButton(
            f"#{l['id']} {l['business_name'][:20]} — {l['status'].replace('_',' ').title()}",
            callback_data=f"upd_lead_{l['id']}"
        )]
        for l in active[:10]
    ]
    await update.message.reply_text(
        "📋 *Select a lead to update:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return UPD_SELECT


async def upd_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lead_id = int(query.data.split("_")[-1])
    ctx.user_data["upd_lead_id"] = lead_id
    keyboard = [
        [InlineKeyboardButton("📞 Contacted",      callback_data="upd_s_contacted")],
        [InlineKeyboardButton("📄 Proposal Sent",  callback_data="upd_s_proposal_sent")],
        [InlineKeyboardButton("🤝 Negotiating",    callback_data="upd_s_negotiating")],
        [InlineKeyboardButton("🔒 Deal Closed",    callback_data="upd_s_deal_closed")],
        [InlineKeyboardButton("❌ Lost",           callback_data="upd_s_lost")],
    ]
    await query.message.reply_text(
        "📊 *Select new status:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return UPD_STATUS


async def upd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ctx.user_data["upd_status"] = query.data.replace("upd_s_", "")
    await query.message.reply_text("📝 Add notes (or type *skip*):", parse_mode="Markdown")
    return UPD_NOTES


async def upd_notes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    notes = update.message.text.strip()
    if notes.lower() == "skip":
        notes = None
    lead_id = ctx.user_data["upd_lead_id"]
    status  = ctx.user_data["upd_status"]
    update_lead_status(lead_id, status, notes)

    if status == "deal_closed" and ADMIN_ID:
        lead = get_lead(lead_id)
        keyboard = [
            [InlineKeyboardButton("✅ Approve", callback_data=f"adm_approve_{lead_id}")],
            [InlineKeyboardButton("❌ Reject",  callback_data=f"adm_reject_{lead_id}")],
        ]
        await update.get_bot().send_message(
            ADMIN_ID,
            f"🔒 *Deal Closed — Needs Approval!*\n\n"
            f"Lead #{lead_id}: {lead['business_name']}\n"
            f"Deal Value: ${lead['deal_value']:,.2f}\n"
            f"Commission: ${lead['commission_amount']:,.2f} ({int(lead['commission_rate']*100)}%)\n"
            f"Notes: {notes or 'None'}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    await update.message.reply_text(
        f"✅ *Lead #{lead_id} → `{status.replace('_', ' ').title()}`*",
        parse_mode="Markdown"
    )
    ctx.user_data.clear()
    return ConversationHandler.END


# ════════════════════════════════════════════════════════════════
# /wallet
# ════════════════════════════════════════════════════════════════

async def cmd_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rep = get_rep(update.effective_user.id)
    if not rep:
        await update.message.reply_text("❌ Register first with /start")
        return

    leads = get_rep_leads(rep["id"])
    pending_leads  = [l for l in leads if l["commission_status"] == "pending" and l["status"] != "lost"]
    approved_leads = [l for l in leads if l["commission_status"] == "approved"]
    paid_leads     = [l for l in leads if l["commission_status"] == "paid"]

    pending_total  = sum(l["commission_amount"] for l in pending_leads)
    approved_total = sum(l["commission_amount"] for l in approved_leads)
    paid_total     = sum(l["commission_amount"] for l in paid_leads)

    msg = (
        f"💰 *Your Blazely Wallet*\n\n"
        f"⏳ *Pending* (awaiting verification)\n"
        f"   ${pending_total:,.2f} across {len(pending_leads)} lead(s)\n\n"
        f"✅ *Approved* (queued for payout)\n"
        f"   ${approved_total:,.2f} across {len(approved_leads)} lead(s)\n\n"
        f"💸 *Paid* (e-transfer sent)\n"
        f"   ${paid_total:,.2f} across {len(paid_leads)} deal(s)\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 *Lifetime Earned: ${rep['total_earned'] or 0:,.2f}*\n"
    )
    if pending_leads:
        msg += "\n*Pending Breakdown:*\n"
        for l in pending_leads[:5]:
            msg += f"  • #{l['id']} {l['business_name']} — ${l['commission_amount']:,.2f} ({int(l['commission_rate']*100)}%)\n"
    msg += "\n_Deal Closed → Payment Verified → Admin Approved → You Get Paid_ 🔥"
    await update.message.reply_text(msg, parse_mode="Markdown")


# ════════════════════════════════════════════════════════════════
# /stats
# ════════════════════════════════════════════════════════════════

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rep = get_rep(update.effective_user.id)
    if not rep:
        await update.message.reply_text("❌ Register first with /start")
        return

    stats  = get_rep_monthly_stats(rep["id"])
    closed = stats["closed_deals"] or 0

    if closed >= 10:
        tier, next_tier = "25% 🏆 Elite", None
    elif closed >= 5:
        tier, next_tier = "22.5% 🥇 Senior", f"{10 - closed} more deals → 25%"
    else:
        tier, next_tier = "20% 🥈 Standard", f"{5 - closed} more deals → 22.5%"

    conv_rate = round((closed / max(stats["total_leads"], 1)) * 100, 1)

    msg = (
        f"📊 *Your Stats — {datetime.now().strftime('%B %Y')}*\n\n"
        f"📥 Leads Logged: {stats['total_leads']}\n"
        f"🔥 Active: {stats['active_leads']}\n"
        f"🤝 Deals Closed: {closed}\n"
        f"📈 Conversion Rate: {conv_rate}%\n"
        f"💸 Earned This Month: ${stats['earned_this_month'] or 0:,.2f}\n\n"
        f"🎯 *Current Tier: {tier}*\n"
    )
    if next_tier:
        msg += f"⚡ _{next_tier}_\n"
    msg += f"\n🏦 *Lifetime Earnings: ${rep['total_earned'] or 0:,.2f}*"
    await update.message.reply_text(msg, parse_mode="Markdown")


# ════════════════════════════════════════════════════════════════
# /leaderboard
# ════════════════════════════════════════════════════════════════

async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    month_start = datetime.now().replace(day=1).isoformat()
    conn = get_db()
    top = conn.execute("""
        SELECT r.full_name,
               COUNT(l.id) as total_leads,
               SUM(CASE WHEN l.status IN
                ('deal_closed','payment_verified','admin_approved',
                 'commission_approved','paid') THEN 1 ELSE 0 END) as closed,
               SUM(CASE WHEN l.commission_status = 'paid'
                THEN l.commission_amount ELSE 0 END) as earned
        FROM reps r
        LEFT JOIN leads l ON r.id = l.rep_id AND l.logged_at >= ?
        WHERE r.is_active = 1
        GROUP BY r.id
        ORDER BY closed DESC, earned DESC
        LIMIT 10
    """, (month_start,)).fetchall()
    conn.close()

    medals = ["🥇", "🥈", "🥉"] + ["🔥"] * 7
    msg = f"🏆 *Blazely Leaderboard — {datetime.now().strftime('%B %Y')}*\n\n"
    for i, r in enumerate(top):
        msg += (f"{medals[i]} *{r['full_name']}*\n"
                f"   {r['closed']} deals · {r['total_leads']} leads · ${r['earned']:,.2f} earned\n\n")
    if not top:
        msg += "_No activity yet. Log your first lead! 🔥_"
    await update.message.reply_text(msg, parse_mode="Markdown")


# ════════════════════════════════════════════════════════════════
# /scripts
# ════════════════════════════════════════════════════════════════

SCRIPTS = {
    "cold_dm": {
        "title": "📲 Cold DM Script",
        "body": (
            "Hey [Name]! 👋 I noticed [Business] doesn't have a website yet — "
            "we just launched Blazely and we build professional landing pages for "
            "local businesses in 24–72 hours for $399–$699. "
            "Would you be open to a quick 5-min call this week? 🔥"
        )
    },
    "cold_call": {
        "title": "📞 Cold Call Opener",
        "body": (
            "Hi, is this [Name]? Great — this is [Your Name] calling from Blazely. "
            "We help local businesses get a professional online presence fast and "
            "affordably — usually under $500 and live in 3 days. "
            "Do you have 2 minutes to hear how it works?"
        )
    },
    "sales_pitch": {
        "title": "🎤 Core Sales Pitch",
        "body": (
            "Most local businesses lose customers every day because they can't be found online. "
            "Blazely fixes that fast.\n\n"
            "✅ Professional landing page, live in 24–72 hours\n"
            "✅ Mobile-optimized, SEO-ready\n"
            "✅ Showcases your services, contact, reviews\n"
            "✅ One-time fee: $399–$699\n\n"
            "No monthly fees. No complicated tech. Just results. 🔥"
        )
    },
    "objection_price": {
        "title": "💰 Objection: Too Expensive",
        "body": (
            "Totally get it. Think about it this way — if your website brings in just ONE "
            "new customer a month, it pays for itself. Most clients see that in week one.\n\n"
            "$499 one-time. No recurring fees. One investment, long-term results. Worth a shot? 💪"
        )
    },
    "objection_time": {
        "title": "⏰ Objection: No Time",
        "body": (
            "That's exactly why we built Blazely — you don't spend any time on it.\n\n"
            "You answer 5 quick questions about your business and we handle everything. "
            "Your page is live in 24–72 hours. Zero effort on your end. 🔥"
        )
    },
    "objection_trust": {
        "title": "🤝 Objection: Is This Legit?",
        "body": (
            "Completely fair. Here's what I'd say:\n\n"
            "We can show you a demo page for your actual business before you pay anything. "
            "You see exactly what you're getting. Love it → we go live. "
            "Don't love it → no hard feelings. Want me to request a free demo? 🎨"
        )
    },
    "follow_up": {
        "title": "🔄 Follow-Up Message",
        "body": (
            "Hey [Name]! Following up from our chat about Blazely. "
            "Have you had a chance to think it over? "
            "I can have a demo page ready for [Business] in a few hours if you'd like to see it first 🔥"
        )
    },
}


async def cmd_scripts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rep = get_rep(update.effective_user.id)
    if not rep:
        await update.message.reply_text("❌ Register first with /start")
        return
    keyboard = [
        [InlineKeyboardButton(s["title"], callback_data=f"script_{key}")]
        for key, s in SCRIPTS.items()
    ]
    await update.message.reply_text(
        "📚 *Blazely Sales Scripts*\n\nPick a script:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def cb_script(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.replace("script_", "")
    script = SCRIPTS.get(key)
    if not script:
        await query.message.reply_text("❌ Script not found.")
        return
    keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="scripts_back")]]
    await query.message.reply_text(
        f"{script['title']}\n\n{script['body']}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def cb_scripts_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton(s["title"], callback_data=f"script_{key}")]
        for key, s in SCRIPTS.items()
    ]
    await query.message.reply_text(
        "📚 *Blazely Sales Scripts*\n\nPick a script:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


# ════════════════════════════════════════════════════════════════
# /demo CONVERSATION
# ════════════════════════════════════════════════════════════════

async def cmd_demo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rep = get_rep(update.effective_user.id)
    if not rep:
        await update.message.reply_text("❌ Register first with /start")
        return ConversationHandler.END
    ctx.user_data["demo_rep"] = rep
    await update.message.reply_text(
        "🎨 *Demo Page Request*\n\nWhat's the *business name*?",
        parse_mode="Markdown"
    )
    return DEMO_BUSINESS


async def demo_business(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    business = update.message.text.strip()
    rep  = ctx.user_data["demo_rep"]
    user = update.effective_user

    conn = get_db()
    lead = conn.execute(
        "SELECT * FROM leads WHERE rep_id = ? AND business_name LIKE ? ORDER BY logged_at DESC LIMIT 1",
        (rep["id"], f"%{business}%")
    ).fetchone()
    if lead:
        conn.execute("UPDATE leads SET demo_requested = 1 WHERE id = ?", (lead["id"],))
        conn.commit()
    conn.close()

    if ADMIN_ID:
        await ctx.bot.send_message(
            ADMIN_ID,
            f"🎨 *Demo Page Requested!*\n\n"
            f"Business: *{business}*\n"
            f"Rep: {rep['full_name']} (@{user.username})\n"
            f"Lead ID: {lead['id'] if lead else 'Not logged yet'}\n\n"
            f"Build a demo and send the link to the rep! ⚡",
            parse_mode="Markdown"
        )
    await update.message.reply_text(
        f"✅ *Demo requested for {business}!*\n\n"
        f"Admin has been notified. You'll receive the demo link shortly.\n\n"
        f"_Keep them warm with the objection handling script while they wait!_",
        parse_mode="Markdown"
    )
    ctx.user_data.clear()
    return ConversationHandler.END


# ════════════════════════════════════════════════════════════════
# /remind
# ════════════════════════════════════════════════════════════════

async def cmd_remind(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rep = get_rep(update.effective_user.id)
    if not rep:
        await update.message.reply_text("❌ Register first with /start")
        return

    cutoff = (datetime.now() - timedelta(days=5)).isoformat()
    conn = get_db()
    stale = conn.execute("""
        SELECT * FROM leads WHERE rep_id = ? AND updated_at < ?
        AND status NOT IN ('deal_closed','payment_verified','admin_approved',
                           'commission_approved','paid','lost')
        ORDER BY updated_at ASC
    """, (rep["id"], cutoff)).fetchall()
    conn.close()

    if not stale:
        await update.message.reply_text("✅ *No stale leads!* You're on top of it. 🔥", parse_mode="Markdown")
        return

    msg = f"⚠️ *{len(stale)} Stale Lead(s) — 5+ Days No Update*\n\n"
    for l in stale:
        days_old = (datetime.now() - datetime.fromisoformat(l["updated_at"])).days
        msg += (f"🔴 *#{l['id']} {l['business_name']}*\n"
                f"   Status: {l['status'].replace('_',' ').title()} · {days_old} days ago\n"
                f"   💰 ${l['deal_value']:,.0f} at stake\n\n")
    msg += "_Use /update — don't let money sit cold! 🔥_"
    await update.message.reply_text(msg, parse_mode="Markdown")


# ════════════════════════════════════════════════════════════════
# AUTO-NUDGE JOB
# ════════════════════════════════════════════════════════════════

async def auto_nudge(ctx: ContextTypes.DEFAULT_TYPE):
    stale = get_stale_leads(days=5)
    notified = {}
    for lead in stale:
        tg_id = lead["telegram_id"]
        if tg_id not in notified:
            notified[tg_id] = []
        notified[tg_id].append(lead)

    for tg_id, leads in notified.items():
        msg = (f"⚠️ *Blazely Reminder* — {len(leads)} lead(s) with no update in 5+ days!\n\n")
        for l in leads[:3]:
            msg += f"🔴 #{l['id']} {l['business_name']} — ${l['deal_value']:,.0f}\n"
        msg += "\n/remind to see full list · /update to move them forward 🔥"
        try:
            await ctx.bot.send_message(tg_id, msg, parse_mode="Markdown")
            conn = get_db()
            conn.execute("""
                UPDATE leads SET last_nudge_at = datetime('now')
                WHERE rep_id = (SELECT id FROM reps WHERE telegram_id = ?)
                AND status NOT IN ('paid','lost','commission_approved')
            """, (tg_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning(f"Could not nudge rep {tg_id}: {e}")


# ════════════════════════════════════════════════════════════════
# /approve (ADMIN)
# ════════════════════════════════════════════════════════════════

async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Admin only.")
        return

    pending = get_pending_approvals()
    if not pending:
        await update.message.reply_text("✅ No pending approvals right now.")
        return

    for l in pending:
        keyboard = [[
            InlineKeyboardButton("✅ Approve", callback_data=f"adm_approve_{l['id']}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"adm_reject_{l['id']}"),
        ]]
        await update.message.reply_text(
            f"🏢 *{l['business_name']}* (Lead #{l['id']})\n"
            f"Rep: {l['full_name']}\n"
            f"Deal: ${l['deal_value']:,.2f}\n"
            f"Commission: ${l['commission_amount']:,.2f} ({int(l['commission_rate']*100)}%)\n"
            f"Type: {l['deal_type'].replace('_',' ').title()}\n"
            f"Source: {'Company' if l['lead_source'] == 'company' else 'Self-sourced'}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )


async def cb_admin_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != ADMIN_ID:
        return

    action  = "approve" if "approve" in query.data else "reject"
    lead_id = int(query.data.split("_")[-1])
    lead    = get_lead(lead_id)
    if not lead:
        await query.message.reply_text("❌ Lead not found.")
        return

    conn = get_db()
    rep  = conn.execute("SELECT * FROM reps WHERE id = ?", (lead["rep_id"],)).fetchone()

    if action == "approve":
        conn.execute("""
            UPDATE leads SET status = 'admin_approved',
            commission_status = 'approved', updated_at = datetime('now')
            WHERE id = ?
        """, (lead_id,))
        conn.execute("""
            UPDATE reps SET
                wallet_pending  = MAX(0, wallet_pending  - ?),
                wallet_approved = wallet_approved + ?
            WHERE id = ?
        """, (lead["commission_amount"], lead["commission_amount"], rep["id"]))
        conn.execute("""
            INSERT INTO admin_approvals (lead_id, rep_id, action) VALUES (?, ?, 'approved')
        """, (lead_id, lead["rep_id"]))
        conn.commit()
        conn.close()
        await query.message.reply_text(
            f"✅ *Deal #{lead_id} APPROVED!*\n"
            f"Commission ${lead['commission_amount']:,.2f} queued for {rep['full_name']}",
            parse_mode="Markdown"
        )
        try:
            await ctx.bot.send_message(
                rep["telegram_id"],
                f"🎉 *Deal Approved!*\n\n"
                f"*{lead['business_name']}* has been approved!\n"
                f"💸 Commission: ${lead['commission_amount']:,.2f}\n\n"
                f"Payment will be processed shortly. Keep closing! 🔥",
                parse_mode="Markdown"
            )
        except Exception as e:
            log.warning(f"Could not notify rep: {e}")
    else:
        conn.execute("""
            UPDATE leads SET commission_status = 'rejected', updated_at = datetime('now')
            WHERE id = ?
        """, (lead_id,))
        conn.execute("""
            INSERT INTO admin_approvals (lead_id, rep_id, action, note)
            VALUES (?, ?, 'rejected', 'Rejected by admin')
        """, (lead_id, lead["rep_id"]))
        conn.commit()
        conn.close()
        await query.message.reply_text(f"❌ Deal #{lead_id} rejected.")


# ════════════════════════════════════════════════════════════════
# /allleads (ADMIN)
# ════════════════════════════════════════════════════════════════

async def cmd_allleads(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Admin only.")
        return

    conn = get_db()
    leads = conn.execute("""
        SELECT l.*, r.full_name FROM leads l
        JOIN reps r ON l.rep_id = r.id
        ORDER BY l.logged_at DESC LIMIT 20
    """).fetchall()
    conn.close()

    if not leads:
        await update.message.reply_text("No leads in the system yet.")
        return

    msg = f"📋 *All Leads (last 20)*\n\n"
    for l in leads:
        msg += (f"#{l['id']} *{l['business_name']}* — {l['full_name']}\n"
                f"   Status: {l['status'].replace('_',' ').title()} · ${l['deal_value']:,.0f}\n\n")
    await update.message.reply_text(msg, parse_mode="Markdown")


# ════════════════════════════════════════════════════════════════
# /broadcast (ADMIN)
# ════════════════════════════════════════════════════════════════

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Admin only.")
        return
    args = update.message.text.partition(" ")[2].strip()
    if not args:
        await update.message.reply_text("Usage: /broadcast Your message here")
        return
    reps = get_all_reps()
    sent, failed = 0, 0
    for rep in reps:
        try:
            await ctx.bot.send_message(rep["telegram_id"], f"📢 *Blazely HQ:*\n\n{args}", parse_mode="Markdown")
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"✅ Broadcast sent to {sent} rep(s). Failed: {failed}")


# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════

def main():
    init_db()
    log.info("🔥 Blazely Bot V3 starting...")

    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation handlers
    newlead_handler = ConversationHandler(
        entry_points=[CommandHandler("newlead", cmd_newlead)],
        states={
            NL_BUSINESS:    [MessageHandler(filters.TEXT & ~filters.COMMAND, nl_business)],
            NL_CONTACT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, nl_contact)],
            NL_SERVICE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, nl_service)],
            NL_PRICE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, nl_price)],
            NL_DEAL_TYPE:   [CallbackQueryHandler(nl_deal_type,   pattern="^dt_")],
            NL_LEAD_SOURCE: [CallbackQueryHandler(nl_lead_source, pattern="^src_")],
        },
        fallbacks=[CommandHandler("cancel", nl_cancel)],
    )

    update_handler = ConversationHandler(
        entry_points=[CommandHandler("update", cmd_update)],
        states={
            UPD_SELECT: [CallbackQueryHandler(upd_select, pattern="^upd_lead_")],
            UPD_STATUS: [CallbackQueryHandler(upd_status, pattern="^upd_s_")],
            UPD_NOTES:  [MessageHandler(filters.TEXT & ~filters.COMMAND, upd_notes)],
        },
        fallbacks=[CommandHandler("cancel", nl_cancel)],
    )

    demo_handler = ConversationHandler(
        entry_points=[CommandHandler("demo", cmd_demo)],
        states={
            DEMO_BUSINESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, demo_business)],
        },
        fallbacks=[CommandHandler("cancel", nl_cancel)],
    )

    # Register handlers
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("myleads",     cmd_myleads))
    app.add_handler(CommandHandler("wallet",      cmd_wallet))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("scripts",     cmd_scripts))
    app.add_handler(CommandHandler("remind",      cmd_remind))
    app.add_handler(CommandHandler("approve",     cmd_approve))
    app.add_handler(CommandHandler("allleads",    cmd_allleads))
    app.add_handler(CommandHandler("broadcast",   cmd_broadcast))
    app.add_handler(newlead_handler)
    app.add_handler(update_handler)
    app.add_handler(demo_handler)

    # Callback handlers
    app.add_handler(CallbackQueryHandler(cb_register,      pattern="^register$"))
    app.add_handler(CallbackQueryHandler(cb_script,        pattern="^script_"))
    app.add_handler(CallbackQueryHandler(cb_scripts_back,  pattern="^scripts_back$"))
    app.add_handler(CallbackQueryHandler(cb_admin_approve, pattern="^adm_(approve|reject)_"))

    # Registration flow message handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_registration))

    # Daily auto-nudge at 10:00 AM
    app.job_queue.run_daily(
        auto_nudge,
        time=datetime.strptime("10:00", "%H:%M").time()
    )

    # Deploy mode
    if DOMAIN:
        log.info(f"🌐 Webhook mode: https://{DOMAIN}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"https://{DOMAIN}/{BOT_TOKEN}",
            secret_token=WEBHOOK_SECRET,
        )
    else:
        log.info("📡 Polling mode")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
