from keep_alive import keep_alive
import asyncio
import logging
import json
import os
import re
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, filters, ContextTypes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════
#            ذاكرة مستمرة (ملفات JSON)
# ══════════════════════════════════════════════════
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

def _path(name): return os.path.join(DATA_DIR, name)

def load_json(name, default):
    p = _path(name)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load {name}: {e}")
    return default

def save_json(name, data):
    try:
        with open(_path(name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save {name}: {e}")

# ══════════════════════════════════════════════════
#              الإعدادات والمعرّفات
# ══════════════════════════════════════════════════
BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID      = 6308107815   # المشرفة
TEACHER_ID    = 5966162893   # المعلمة
GROUP_CHAT_ID = -1002940636525
MAX_UNEXCUSED = 3

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Please add it to Replit Secrets.")

# ══════════════════════════════════════════════════
#         البيانات الدائمة (محفوظة على القرص)
# ══════════════════════════════════════════════════
user_ids_map       = load_json("users_map.json", {})
TOTAL_STUDENTS     = load_json("students.json", ["@AnnaAnnaHA"])
unexcused_absences = load_json("unexcused.json", {})
scheduled_session  = load_json("scheduled_session.json", {})  # {day_of_week, hour, minute}
session_history    = load_json("session_history.json", [])    # list of completed session records
faq_items          = load_json("faq.json", [
    {"id": 1, "q": "متى موعد الحصة القادمة؟",  "a": "سيتم الإعلان عنها قريباً إن شاء الله."},
    {"id": 2, "q": "كيف أسجل غيابي بعذر؟",     "a": "استخدمي زر الاعتذار لحظة بدء الحصة، أو تواصلي مع المشرفة."},
    {"id": 3, "q": "ما هي متطلبات الحصة؟",      "a": "الحضور والانتباه والمصحف الشريف."},
])

# ══════════════════════════════════════════════════
#         البيانات المؤقتة (في الذاكرة)
# ══════════════════════════════════════════════════
checked_in_students          = set()
final_statuses               = {}
session_details              = {"free_text": "لم تُحدد تفاصيل بعد"}
session_active               = False
session_start_datetime       = ""
session_start_date_key       = ""
published_reports            = {}
teacher_report_input_pending = {}
add_student_pending          = set()
remove_student_pending       = set()
inquiry_pending              = set()
admin_reply_pending          = {}
faq_edit_pending             = {}
faq_add_pending              = set()
schedule_pending_time        = {}   # user_id -> day_of_week int waiting for time text
post_pending                 = set()  # user_ids currently composing a group post

ARABIC_DAYS = {
    0: "الإثنين", 1: "الثلاثاء", 2: "الأربعاء",
    3: "الخميس",  4: "الجمعة",   5: "السبت",  6: "الأحد"
}
DAY_NAMES_CRON = {0:"mon", 1:"tue", 2:"wed", 3:"thu", 4:"fri", 5:"sat", 6:"sun"}
ARABIC_MONTHS  = {
    1:"يناير", 2:"فبراير", 3:"مارس", 4:"أبريل",
    5:"مايو",  6:"يونيو",  7:"يوليو", 8:"أغسطس",
    9:"سبتمبر",10:"أكتوبر",11:"نوفمبر",12:"ديسمبر"
}

application: Application = None
scheduler   = AsyncIOScheduler()

# ══════════════════════════════════════════════════
#              دوال مساعدة
# ══════════════════════════════════════════════════
def algeria_now():
    return datetime.utcnow() + timedelta(hours=1)

def format_session_datetime():
    now = algeria_now()
    return f"{ARABIC_DAYS[now.weekday()]} {now.strftime('%Y-%m-%d')} — الساعة {now.strftime('%H:%M')} (توقيت الجزائر)"

def clean_username(uname):
    if not uname: return ""
    uname = str(uname).strip().lower()
    if not uname.startswith("@"):
        uname = "@" + uname
    return uname

def build_report_text(students, statuses, checked_in, free_text, dt_str):
    present, excused, absent = [], [], []
    for s in students:
        s_clean = clean_username(s)
        st = statuses.get(s_clean, "حضور" if s_clean in checked_in else "غياب بغير عذر")
        if st in ("حضور", "حضور تام", "حضور جزئي"):
            present.append(s)
        elif st == "غياب بعذر":
            excused.append(s)
        else:
            absent.append(s)
    return (
        f"بسم الله الرحمان الرحيم\n🌴 مقرأ الإمام نافع 🌴\n\n"
        f"الأستاذة المقرئة: 👑 العصماء 👑\n🗓 التاريخ: {dt_str}\n\n"
        f"📚 تفاصيل الحصة:\n{free_text}\n\n"
        f"📝 القائمة الاسمية:\n\n"
        f"✅ حضور: {', '.join(present) if present else 'لا يوجد'}\n"
        f"🟢 غياب بعذر: {', '.join(excused) if excused else 'لا يوجد'}\n"
        f"🔴 غياب بغير عذر: {', '.join(absent) if absent else 'لا يوجد'}"
    )

def get_faq_keyboard():
    rows = [[InlineKeyboardButton(item["q"], callback_data=f"faq_{item['id']}")] for item in faq_items]
    rows.append([InlineKeyboardButton("💬 سؤال آخر", callback_data="faq_custom")])
    return InlineKeyboardMarkup(rows)

def get_faq_admin_keyboard():
    rows = [[InlineKeyboardButton(f"✏️ {item['q']}", callback_data=f"faq_edit_{item['id']}")] for item in faq_items]
    rows.append([InlineKeyboardButton("➕ إضافة سؤال جديد", callback_data="faq_add_new")])
    return InlineKeyboardMarkup(rows)

def get_day_picker_keyboard():
    rows = []
    row = []
    for day_num, day_name in ARABIC_DAYS.items():
        row.append(InlineKeyboardButton(day_name, callback_data=f"sched_day_{day_num}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="sched_cancel")])
    return InlineKeyboardMarkup(rows)

def closing_message_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❓ طرح استفسار", callback_data="post_session_faq")
    ]])

def get_month_picker_keyboard():
    now = algeria_now()
    rows = []
    # offer current month + up to 5 previous months that have data
    months_with_data = set()
    for rec in session_history:
        dk = rec.get("date_key", "")
        if len(dk) == 8:
            months_with_data.add(dk[:6])  # YYYYMM
    # always include current and last month
    options = []
    for delta in range(6):
        d = now.replace(day=1) - timedelta(days=delta * 28)
        ym = d.strftime("%Y%m")
        label = f"{ARABIC_MONTHS[d.month]} {d.year}"
        options.append((ym, label))
    # deduplicate preserving order
    seen = set()
    unique = []
    for ym, label in options:
        if ym not in seen:
            seen.add(ym)
            unique.append((ym, label))
    for i in range(0, len(unique), 2):
        row = []
        for ym, label in unique[i:i+2]:
            count = sum(1 for r in session_history if r.get("date_key", "")[:6] == ym)
            suffix = f" ({count})" if count else " (لا يوجد)"
            row.append(InlineKeyboardButton(label + suffix, callback_data=f"monthly_{ym}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="monthly_cancel")])
    return InlineKeyboardMarkup(rows)

def build_monthly_report(year_month: str) -> str:
    """year_month = 'YYYYMM'. Returns formatted report text."""
    year  = int(year_month[:4])
    month = int(year_month[4:])
    month_ar = ARABIC_MONTHS.get(month, year_month)

    sessions = [r for r in session_history if r.get("date_key", "")[:6] == year_month]
    total_sessions = len(sessions)

    if total_sessions == 0:
        return f"📈 تقرير شهر {month_ar} {year}\n\nلم تُسجَّل أي حصة في هذا الشهر."

    # Collect all students who ever appeared
    all_students = list(TOTAL_STUDENTS)
    for rec in sessions:
        for s in rec.get("students", []):
            if clean_username(s) not in [clean_username(x) for x in all_students]:
                all_students.append(s)

    lines = [
        f"بسم الله الرحمان الرحيم",
        f"🌴 مقرأ الإمام نافع 🌴",
        f"",
        f"📈 التقرير الشهري — {month_ar} {year}",
        f"عدد الحصص المنعقدة: {total_sessions}",
        f"",
        f"{'الطالبة':<20} {'✅ حضور':>8} {'🟢 بعذر':>8} {'🔴 بدون عذر':>12} {'نسبة الحضور':>12}",
        "─" * 62,
    ]

    for s in all_students:
        sc = clean_username(s)
        present = excused = absent = 0
        for rec in sessions:
            statuses  = {clean_username(k): v for k, v in rec.get("statuses", {}).items()}
            checked   = set(clean_username(x) for x in rec.get("checked_in", []))
            st = statuses.get(sc, "حضور" if sc in checked else "غياب بغير عذر")
            if st in ("حضور", "حضور تام", "حضور جزئي"):
                present += 1
            elif st == "غياب بعذر":
                excused += 1
            else:
                absent += 1
        pct = round((present / total_sessions) * 100) if total_sessions else 0
        bar_filled = round(pct / 10)
        bar = "🟩" * bar_filled + "⬜" * (10 - bar_filled)
        lines.append(
            f"{s}\n"
            f"  ✅ {present}  🟢 {excused}  🔴 {absent}  |  {bar}  {pct}%\n"
        )

    lines.append("─" * 62)
    lines.append(f"📅 تاريخ إصدار التقرير: {algeria_now().strftime('%Y-%m-%d')}")
    return "\n".join(lines)

ADMIN_KB = ReplyKeyboardMarkup([
    [KeyboardButton("📢 إرسال تذكير الساعة"), KeyboardButton("🚀 بدء الحصة والنداء")],
    [KeyboardButton("📋 عرض الحضور الآن"),   KeyboardButton("🏁 نهاية الحصة رسمياً")],
    [KeyboardButton("➕ إضافة طالبة"),        KeyboardButton("➖ حذف طالبة")],
    [KeyboardButton("📊 إحصائيات الغياب"),    KeyboardButton("📋 قائمة الطالبات")],
    [KeyboardButton("🗓 برمجة حصة"),          KeyboardButton("⚙️ إدارة الأسئلة الشائعة")],
    [KeyboardButton("📈 تقرير شهري")],
    [KeyboardButton("📣 إنشاء منشور جديد في المجموعة")],
], resize_keyboard=True)

# ══════════════════════════════════════════════════
#         جدولة التذكيرات التلقائية
# ══════════════════════════════════════════════════
async def job_remind_group():
    """يُرسل للمجموعة تذكيراً قبل ساعة من الحصة"""
    try:
        await application.bot.send_message(
            GROUP_CHAT_ID,
            "👋 السلام عليكم ورحمة الله وبركاته\n"
            "⏰ ستبدأ الحصة بعد ساعة، الرجاء الاستعداد والالتزام بالميعاد يا غاليات. 🌴"
        )
        logger.info("Scheduled group reminder sent.")
    except Exception as e:
        logger.error(f"Failed to send scheduled group reminder: {e}")

async def job_remind_teacher():
    """يُرسل للمشرفة والمعلمة تذكيراً خاصاً قبل 5 دقائق من الحصة"""
    try:
        ss = scheduled_session
        day_ar = ARABIC_DAYS.get(ss.get("day_of_week", 0), "")
        time_str = f"{ss.get('hour', 0):02d}:{ss.get('minute', 0):02d}"
        msg = (
            f"⏰ تذكير خاص: الحصة ستبدأ بعد 5 دقائق\n"
            f"🗓 {day_ar} — الساعة {time_str} (توقيت الجزائر)\n\n"
            f"بالتوفيق والسداد 🌴"
        )
        for uid in {ADMIN_ID, TEACHER_ID}:
            try:
                await application.bot.send_message(uid, msg)
            except Exception as e:
                logger.warning(f"Could not send reminder to {uid}: {e}")
        logger.info("Scheduled teacher/admin reminder sent.")
    except Exception as e:
        logger.error(f"Failed to send scheduled teacher reminder: {e}")

def subtract_time(hour, minute, delta_minutes):
    """Returns (hour, minute) after subtracting delta_minutes"""
    total = hour * 60 + minute - delta_minutes
    total = total % (24 * 60)
    return total // 60, total % 60

def apply_scheduled_jobs():
    """Reads scheduled_session and registers/replaces APScheduler jobs."""
    if not scheduled_session:
        return
    dow  = scheduled_session["day_of_week"]
    h    = scheduled_session["hour"]
    m    = scheduled_session["minute"]
    cron_day = DAY_NAMES_CRON[dow]

    # جهة المجموعة: قبل ساعة
    gh, gm = subtract_time(h, m, 60)
    # جهة المشرفة: قبل 5 دقائق
    th, tm = subtract_time(h, m, 5)

    for job_id in ("remind_group", "remind_teacher"):
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

    scheduler.add_job(
        job_remind_group,
        CronTrigger(day_of_week=cron_day, hour=gh, minute=gm, timezone="Africa/Algiers"),
        id="remind_group",
        replace_existing=True
    )
    scheduler.add_job(
        job_remind_teacher,
        CronTrigger(day_of_week=cron_day, hour=th, minute=tm, timezone="Africa/Algiers"),
        id="remind_teacher",
        replace_existing=True
    )
    logger.info(f"Scheduled: group@{gh:02d}:{gm:02d}, teacher@{th:02d}:{tm:02d} every {cron_day}")

async def update_published_report(date_key, student_clean, new_status):
    if date_key not in published_reports:
        return
    rpt = published_reports[date_key]
    rpt["statuses"][clean_username(student_clean)] = new_status
    normalized_statuses = {clean_username(k): v for k, v in rpt["statuses"].items()}
    new_text = build_report_text(
        rpt["students"], normalized_statuses, rpt["checked_in"],
        rpt["free_text"], rpt["dt_str"]
    )
    try:
        await application.bot.edit_message_text(
            text=new_text, chat_id=GROUP_CHAT_ID, message_id=rpt["msg_id"]
        )
    except Exception as e:
        logger.error(f"Failed to edit live report message: {e}")

# ══════════════════════════════════════════════════
#            معالج الرسائل الخاصة
# ══════════════════════════════════════════════════
async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global session_active, checked_in_students, final_statuses
    global session_start_datetime, session_start_date_key
    global TOTAL_STUDENTS, faq_items, scheduled_session, post_pending

    message = update.message
    if not message:
        return
    user_id  = message.from_user.id
    username = message.from_user.username
    user_idx = f"@{username}" if username else str(user_id)
    text     = (message.text or "").strip()
    is_admin   = user_id == ADMIN_ID
    is_teacher = user_id == TEACHER_ID
    is_staff   = is_admin or is_teacher

    if username:
        user_ids_map[clean_username(user_idx)] = user_id
        save_json("users_map.json", user_ids_map)

    # ── وضع النشر في المجموعة (معلمة أو مشرفة) ──
    if is_staff and user_id in post_pending:
        if text in ("إنهاء", "إنهاء النشر", "انتهى", "إلغاء"):
            post_pending.discard(user_id)
            await message.reply_text("✅ تم إنهاء جلسة النشر.", reply_markup=ADMIN_KB)
            return
        try:
            await application.bot.copy_message(
                chat_id=GROUP_CHAT_ID,
                from_chat_id=message.chat_id,
                message_id=message.message_id
            )
            await message.reply_text(
                "✅ تم نشر الرسالة في المجموعة.\n"
                "يمكنكِ إرسال المزيد، أو اكتبي «إنهاء» للخروج من وضع النشر."
            )
        except Exception as e:
            await message.reply_text(f"❌ تعذر النشر في المجموعة: {e}")
        return

    # ── ردّ إداري على طالبة (المشرفة أو المعلمة) ──
    if is_staff and user_id in admin_reply_pending:
        t_id = admin_reply_pending.pop(user_id)
        try:
            await application.bot.send_message(
                chat_id=int(t_id),
                text=f"💬 إجابة من إدارة مقرأ الإمام نافع:\n\n{text}"
            )
            await message.reply_text("✅ تم إرسال إجابتكِ للطالبة!")
        except Exception as e:
            await message.reply_text(f"❌ تعذر الإرسال: {e}")
        return

    # ── إدخال وقت الحصة المجدولة ──
    if is_staff and user_id in schedule_pending_time:
        day_of_week = schedule_pending_time.pop(user_id)
        match = re.match(r"^(\d{1,2}):(\d{2})$", text)
        if not match:
            await message.reply_text(
                "❌ صيغة الوقت غير صحيحة. يرجى إرسال الوقت بهذا الشكل: 20:30",
                reply_markup=ADMIN_KB
            )
            return
        h, m = int(match.group(1)), int(match.group(2))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            await message.reply_text("❌ الوقت غير صالح. مثال صحيح: 20:30", reply_markup=ADMIN_KB)
            return
        scheduled_session = {"day_of_week": day_of_week, "hour": h, "minute": m}
        save_json("scheduled_session.json", scheduled_session)
        apply_scheduled_jobs()
        day_ar = ARABIC_DAYS[day_of_week]
        gh, gm = subtract_time(h, m, 60)
        th, tm = subtract_time(h, m, 5)
        await message.reply_text(
            f"✅ تم جدولة الحصة بنجاح!\n\n"
            f"🗓 اليوم: {day_ar}\n"
            f"🕐 وقت الحصة: {h:02d}:{m:02d}\n\n"
            f"📢 تذكير للمجموعة: {gh:02d}:{gm:02d} (قبل ساعة)\n"
            f"👑 تذكير خاص للمشرفة والمعلمة: {th:02d}:{tm:02d} (قبل 5 دقائق)\n\n"
            f"سيُرسل هذا التذكير تلقائياً كل أسبوع. 🌴",
            reply_markup=ADMIN_KB
        )
        return

    # ── تعديل سؤال شائع ──
    if is_staff and user_id in faq_edit_pending:
        faq_id = faq_edit_pending.pop(user_id)
        parts = text.split("\n", 1)
        if len(parts) == 2:
            q, a = parts[0].strip(), parts[1].strip()
            for item in faq_items:
                if item["id"] == faq_id:
                    item["q"] = q
                    item["a"] = a
                    break
            save_json("faq.json", faq_items)
            await message.reply_text("✅ تم تحديث السؤال الشائع.", reply_markup=ADMIN_KB)
        else:
            await message.reply_text(
                "❌ الصيغة غير صحيحة. يرجى إرسال السؤال في السطر الأول والجواب في السطر الثاني."
            )
        return

    # ── إضافة سؤال شائع جديد ──
    if is_staff and user_id in faq_add_pending:
        faq_add_pending.discard(user_id)
        parts = text.split("\n", 1)
        if len(parts) == 2:
            q, a = parts[0].strip(), parts[1].strip()
            new_id = max((item["id"] for item in faq_items), default=0) + 1
            faq_items.append({"id": new_id, "q": q, "a": a})
            save_json("faq.json", faq_items)
            await message.reply_text("✅ تمت إضافة السؤال الشائع الجديد.", reply_markup=ADMIN_KB)
        else:
            await message.reply_text(
                "❌ الصيغة غير صحيحة. يرجى إرسال السؤال في السطر الأول والجواب في السطر الثاني."
            )
        return

    # ── إلغاء ──
    if text == "إلغاء" and is_staff:
        add_student_pending.discard(user_id)
        remove_student_pending.discard(user_id)
        faq_edit_pending.pop(user_id, None)
        faq_add_pending.discard(user_id)
        schedule_pending_time.pop(user_id, None)
        post_pending.discard(user_id)
        await message.reply_text("❌ تم إلغاء العملية.", reply_markup=ADMIN_KB)
        return

    # ── لوحة التحكم (المشرفة والمعلمة) ──
    if text in ("لوحة التحكم", "/start", "/panel") and is_staff:
        await message.reply_text("👑 مرحباً في لوحة التحكم الإدارية للمقرأ:", reply_markup=ADMIN_KB)
        return

    # ── ترحيب الطالبات ──
    if text in ("/start", "/inquiry") and not is_staff:
        await message.reply_text(
            "👋 السلام عليكم ورحمة الله وبركاته في مقرأ الإمام نافع!\nإليكِ قائمة الأسئلة المقترحة:",
            reply_markup=get_faq_keyboard()
        )
        return

    # ══ أوامر الإدارة (مشرفة ومعلمة) ══

    if text == "📢 إرسال تذكير الساعة" and is_staff:
        try:
            await application.bot.send_message(
                GROUP_CHAT_ID,
                "👋 السلام عليكم ورحمة الله وبركاته\n"
                "⏰ ستبدأ الحصة بعد ساعة، الرجاء الاستعداد والالتزام بالميعاد يا غاليات."
            )
            await message.reply_text("✅ تم نشر تنبيه الساعة!")
        except Exception as e:
            await message.reply_text(f"❌ تعذر الإرسال: {e}")

    elif text == "🚀 بدء الحصة والنداء" and is_staff:
        session_active         = True
        session_start_datetime = format_session_datetime()
        session_start_date_key = algeria_now().strftime("%Y%m%d")
        checked_in_students.clear()
        final_statuses.clear()
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📍 سجلي حضوري",       callback_data="student_checkin"),
            InlineKeyboardButton("⚠️ أعتذر عن الحصة", callback_data="student_apology")
        ]])
        try:
            await application.bot.send_message(
                GROUP_CHAT_ID,
                "📢 بدأت الحلقة هلموا يا غاليات!\n\n"
                "📍 سجّلي حضوركِ، أو اضغطي زر الاعتذار إن كان لديكِ ظرف طارئ:",
                reply_markup=kb
            )
            await message.reply_text("🚀 انطلقت الحصة!")
        except Exception as e:
            await message.reply_text(f"❌ تعذر الإرسال للمجموعة: {e}")

    elif text == "📋 عرض الحضور الآن" and is_staff:
        if not TOTAL_STUDENTS:
            await message.reply_text("⚠️ لا توجد طالبات مسجلات حالياً.")
            return
        lines = []
        for s in TOTAL_STUDENTS:
            sc = clean_username(s)
            st = final_statuses.get(sc, "حضور" if sc in checked_in_students else "غائبة")
            icon = "✅" if "حضور" in st else ("🟢" if "عذر" in st else "❌")
            lines.append(f"{icon} {s}: {st}")
        status = "🟢 الحصة نشطة" if session_active else "🔴 لا توجد حصة نشطة"
        await message.reply_text(f"📋 الحضور الحالي ({status}):\n\n" + "\n".join(lines))

    elif text == "🏁 نهاية الحصة رسمياً" and is_staff:
        if not session_active:
            await message.reply_text("❌ لا توجد حصة نشطة حالياً.")
            return
        session_active = False
        teacher_report_input_pending[TEACHER_ID] = True
        await message.reply_text("🏁 تم إنهاء الحصة. سيصل طلب تفاصيل الدرس إلى المعلمة.")
        try:
            await application.bot.send_message(
                TEACHER_ID,
                "🏁 تم إنهاء الحصة.\n\nيرجى كتابة عنوان وعناصر الدرس الآن (سيُنشر في المحضر):"
            )
        except Exception as e:
            await message.reply_text(f"⚠️ تعذر إرسال الطلب للمعلمة: {e}")

    elif text == "➕ إضافة طالبة" and is_staff:
        add_student_pending.add(user_id)
        await message.reply_text(
            "📝 اكتبي اسم المستخدم لإضافتها (مثال: @Sara2025)\n\nأو اكتبي «إلغاء» للتراجع."
        )

    elif text == "➖ حذف طالبة" and is_staff:
        remove_student_pending.add(user_id)
        current = "\n".join(TOTAL_STUDENTS) if TOTAL_STUDENTS else "لا توجد طالبات"
        await message.reply_text(
            f"📝 اكتبي اسم المستخدم للحذف:\n\nالطالبات الحاليات:\n{current}\n\nأو اكتبي «إلغاء» للتراجع."
        )

    elif text == "📋 قائمة الطالبات" and is_staff:
        if not TOTAL_STUDENTS:
            await message.reply_text("⚠️ لا توجد طالبات مسجلات حالياً.")
        else:
            lines = [f"{i+1}. {s}" for i, s in enumerate(TOTAL_STUDENTS)]
            await message.reply_text(f"📋 قائمة الطالبات ({len(TOTAL_STUDENTS)}):\n\n" + "\n".join(lines))

    elif text == "📊 إحصائيات الغياب" and is_staff:
        lines = []
        for s in TOTAL_STUDENTS:
            sc = clean_username(s)
            count = unexcused_absences.get(sc, 0)
            if count > 0:
                bar = "🔴" * count + "⬜" * max(0, MAX_UNEXCUSED - count)
                lines.append(f"{s}: {bar} ({count}/{MAX_UNEXCUSED})")
        if not lines:
            await message.reply_text("✅ لا توجد غيابات بغير عذر مسجلة.")
        else:
            await message.reply_text("📊 إحصائيات الغياب بغير عذر:\n\n" + "\n".join(lines))

    elif text == "🗓 برمجة حصة" and is_staff:
        info = ""
        if scheduled_session:
            dow = scheduled_session.get("day_of_week", 0)
            h   = scheduled_session.get("hour", 0)
            m   = scheduled_session.get("minute", 0)
            info = (
                f"\n\n📌 الجدول الحالي: {ARABIC_DAYS[dow]} الساعة {h:02d}:{m:02d}\n"
                f"يمكنكِ تغييره باختيار يوم جديد."
            )
        await message.reply_text(
            f"🗓 اختاري يوم الحصة الأسبوعية:{info}",
            reply_markup=get_day_picker_keyboard()
        )

    elif text == "📈 تقرير شهري" and is_staff:
        await message.reply_text(
            "📈 اختاري الشهر لعرض تقرير الحضور التفصيلي:\n"
            "(الرقم بين القوسين = عدد الحصص المسجلة)",
            reply_markup=get_month_picker_keyboard()
        )

    elif text == "⚙️ إدارة الأسئلة الشائعة" and is_staff:
        if not faq_items:
            await message.reply_text(
                "⚙️ لا توجد أسئلة شائعة حالياً.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ إضافة سؤال جديد", callback_data="faq_add_new")
                ]])
            )
        else:
            await message.reply_text(
                "⚙️ اختاري سؤالاً للتعديل أو أضيفي سؤالاً جديداً:",
                reply_markup=get_faq_admin_keyboard()
            )

    elif text == "📣 إنشاء منشور جديد في المجموعة" and is_staff:
        post_pending.add(user_id)
        await message.reply_text(
            "📣 وضع النشر في المجموعة:\n\n"
            "أرسلي رسالتك الآن — نصاً أو صورة أو ملفاً أو فيديو، وسيُنشر فوراً في المجموعة باسم البوت.\n\n"
            "يمكنكِ إرسال عدة رسائل متتالية.\n"
            "اكتبي «إنهاء» عند الانتهاء."
        )

    elif user_id in add_student_pending:
        add_student_pending.discard(user_id)
        u = text if text.startswith("@") else "@" + text
        if clean_username(u) not in [clean_username(s) for s in TOTAL_STUDENTS]:
            TOTAL_STUDENTS.append(u)
            save_json("students.json", TOTAL_STUDENTS)
            await message.reply_text(f"✅ تمت إضافة {u} إلى قائمة الطالبات.", reply_markup=ADMIN_KB)
        else:
            await message.reply_text(f"⚠️ الطالبة {u} موجودة بالفعل في القائمة.", reply_markup=ADMIN_KB)

    elif user_id in remove_student_pending:
        remove_student_pending.discard(user_id)
        u = text if text.startswith("@") else "@" + text
        before = len(TOTAL_STUDENTS)
        TOTAL_STUDENTS[:] = [s for s in TOTAL_STUDENTS if clean_username(s) != clean_username(u)]
        save_json("students.json", TOTAL_STUDENTS)
        if len(TOTAL_STUDENTS) < before:
            await message.reply_text(f"✅ تم حذف {u} من القائمة.", reply_markup=ADMIN_KB)
        else:
            await message.reply_text(f"⚠️ لم يتم إيجاد الطالبة {u} في القائمة.", reply_markup=ADMIN_KB)

    elif teacher_report_input_pending.get(user_id):
        teacher_report_input_pending[user_id] = False
        session_details["free_text"] = text
        txt = build_report_text(
            TOTAL_STUDENTS, final_statuses, checked_in_students, text, session_start_datetime
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 اعتماد ونشر المحضر", callback_data="publish_report_final")
        ]])
        await message.reply_text(f"📊 مسودة المحضر للمراجعة:\n\n{txt}", reply_markup=kb)

    elif clean_username(user_idx) in inquiry_pending:
        inquiry_pending.discard(clean_username(user_idx))
        await application.bot.send_message(
            ADMIN_ID,
            f"❓ استفسار مخصص جديد\n\nمن: {user_idx}\nالنص: {text}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💬 الرد على الطالبة", callback_data=f"reply_to_{user_id}")
            ]])
        )
        await message.reply_text("✅ تم إرسال استفساركِ بنجاح. سنردّ عليكِ قريباً إن شاء الله.")

    else:
        if not is_staff:
            await message.reply_text(
                "👋 مرحباً! لأي استفسار استخدمي الأزرار أدناه:",
                reply_markup=get_faq_keyboard()
            )

# ══════════════════════════════════════════════════
#           معالج جميع الأزرار التفاعلية
# ══════════════════════════════════════════════════
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global faq_items, scheduled_session, session_history

    query      = update.callback_query
    data       = query.data
    user_id    = query.from_user.id
    username   = query.from_user.username
    user_idx   = f"@{username}" if username else str(user_id)
    is_admin   = user_id == ADMIN_ID
    is_teacher = user_id == TEACHER_ID
    is_staff   = is_admin or is_teacher

    if username:
        user_ids_map[clean_username(user_idx)] = user_id
        save_json("users_map.json", user_ids_map)

    norm_user = clean_username(user_idx)

    try:
        # ── رد إداري ──
        if data.startswith("reply_to_") and is_staff:
            target_id = int(data.split("_")[2])
            admin_reply_pending[user_id] = target_id
            await application.bot.send_message(
                user_id, "📝 تفضلي بكتابة ردّكِ في المحادثة الخاصة مباشرة:"
            )
            await query.answer("سيتم فتح المحادثة الخاصة لكتابة الرد.")
            return

        # ── برمجة حصة: اختيار اليوم ──
        if data.startswith("sched_day_") and is_staff:
            day_num = int(data[10:])
            schedule_pending_time[user_id] = day_num
            day_ar = ARABIC_DAYS[day_num]
            await query.answer()
            await application.bot.send_message(
                user_id,
                f"✅ اخترتِ يوم {day_ar}.\n\n"
                "⏰ الآن أرسلي وقت الحصة بالصيغة التالية:\n"
                "مثال: 20:30\n\n"
                "أو اكتبي «إلغاء» للتراجع."
            )
            return

        if data == "sched_cancel":
            schedule_pending_time.pop(user_id, None)
            await query.answer("تم الإلغاء.")
            await query.message.edit_text("❌ تم إلغاء برمجة الحصة.")
            return

        # ── التقرير الشهري: اختيار الشهر ──
        if data == "monthly_cancel":
            await query.answer("تم الإلغاء.")
            await query.message.edit_text("❌ تم إلغاء طلب التقرير.")
            return

        if data.startswith("monthly_") and is_staff:
            ym = data[8:]  # YYYYMM
            await query.answer()
            await query.message.edit_text("⏳ جارٍ إعداد التقرير...")
            report_text = build_monthly_report(ym)
            # Telegram messages max 4096 chars — split if needed
            if len(report_text) <= 4096:
                await application.bot.send_message(ADMIN_ID, report_text)
            else:
                for i in range(0, len(report_text), 4000):
                    await application.bot.send_message(ADMIN_ID, report_text[i:i+4000])
            return

        # ── تسجيل الحضور ──
        if data == "student_checkin":
            if not session_active:
                await query.answer("⚠️ الحصة لم تبدأ بعد أو انتهت.", show_alert=True)
                return
            checked_in_students.add(norm_user)
            final_statuses.pop(norm_user, None)
            await query.answer("✅ تم تسجيل حضوركِ بنجاح!", show_alert=True)

        # ── الاعتذار ──
        elif data == "student_apology":
            if not session_active:
                await query.answer("⚠️ الحصة لم تبدأ بعد.", show_alert=True)
                return
            final_statuses[norm_user] = "غياب بعذر"
            checked_in_students.discard(norm_user)
            await query.answer("✅ تم تسجيل اعتذاركِ بنجاح.", show_alert=True)

        # ── نشر المحضر ──
        elif data == "publish_report_final":
            report_body = query.message.text.replace("📊 مسودة المحضر للمراجعة:\n\n", "")
            sent_msg = await application.bot.send_message(GROUP_CHAT_ID, report_body)
            await query.message.edit_text("✅ تم نشر المحضر في المجموعة بنجاح!")

            dk = session_start_date_key or algeria_now().strftime("%Y%m%d")
            normalized_statuses = {clean_username(k): v for k, v in final_statuses.items()}
            if sent_msg:
                published_reports[dk] = {
                    "msg_id":    sent_msg.message_id,
                    "students":  list(TOTAL_STUDENTS),
                    "statuses":  normalized_statuses,
                    "checked_in": list(checked_in_students),
                    "free_text": session_details.get("free_text", ""),
                    "dt_str":    session_start_datetime,
                }
                # حفظ سجل الحصة في التاريخ الدائم
                history_record = {
                    "date_key":  dk,
                    "dt_str":    session_start_datetime,
                    "students":  list(TOTAL_STUDENTS),
                    "statuses":  normalized_statuses,
                    "checked_in": list(checked_in_students),
                    "free_text": session_details.get("free_text", ""),
                }
                # تحديث إن كان السجل موجوداً مسبقاً، أو إضافة جديد
                existing = next((i for i, r in enumerate(session_history) if r.get("date_key") == dk), None)
                if existing is not None:
                    session_history[existing] = history_record
                else:
                    session_history.append(history_record)
                save_json("session_history.json", session_history)

            # رسالة الختام في المجموعة مع زر "طرح استفسار"
            await application.bot.send_message(
                GROUP_CHAT_ID,
                "جزاكم الله خيراً على حضور الحصة ونفعكم بها، نلقاكم الحلقة القادمة بحول الله 🕊❤️\n\n"
                "هل لديكِ استفسار؟ يمكنكِ طرحه بالضغط على الزر أدناه:",
                reply_markup=closing_message_keyboard()
            )

            # إرسال إشعار للغائبات بدون عذر
            for student in TOTAL_STUDENTS:
                s_clean = clean_username(student)
                if normalized_statuses.get(s_clean) == "غياب بعذر":
                    continue
                eff = normalized_statuses.get(
                    s_clean, "حضور" if s_clean in checked_in_students else "غياب بغير عذر"
                )
                if eff == "غياب بغير عذر":
                    t_id = user_ids_map.get(s_clean)
                    if t_id:
                        ask_kb = InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ نعم، كان لدي عذر",    callback_data=f"exc_y_{dk}_{s_clean}"),
                            InlineKeyboardButton("❌ لا، لم يكن لدي عذر", callback_data=f"exc_n_{dk}_{s_clean}")
                        ]])
                        try:
                            await application.bot.send_message(
                                chat_id=int(t_id),
                                text=(
                                    "📋 السلام عليكم ورحمة الله وبركاته.\n"
                                    "لاحظنا غيابكِ عن حلقة اليوم دون إشعار مسبق.\n"
                                    "هل كان لديكِ عذر قاهر؟"
                                ),
                                reply_markup=ask_kb
                            )
                        except Exception as e:
                            logger.error(f"Failed to notify {s_clean}: {e}")
                    else:
                        logger.warning(f"No user ID for {s_clean} — they haven't started the bot yet")

        # ── زر "طرح استفسار" بعد الختام (من المجموعة) ──
        elif data == "post_session_faq":
            if not faq_items:
                await query.answer("لا توجد أسئلة شائعة حالياً.", show_alert=True)
                return
            try:
                await application.bot.send_message(
                    user_id,
                    "❓ اختاري سؤالاً من القائمة، أو اضغطي «سؤال آخر» لطرح سؤالك بنفسكِ:",
                    reply_markup=get_faq_keyboard()
                )
                await query.answer("تم فتح قائمة الأسئلة في المحادثة الخاصة.")
            except Exception:
                await query.answer(
                    "⚠️ يرجى فتح المحادثة الخاصة مع البوت أولاً ثم اضغطي مجدداً.",
                    show_alert=True
                )

        # ── قبول العذر بعد النشر ──
        elif data.startswith("exc_y_"):
            rest = data[6:]
            dk, student_clean = rest[:8], rest[9:]
            final_statuses[clean_username(student_clean)] = "غياب بعذر"
            await update_published_report(dk, student_clean, "غياب بعذر")
            await query.message.edit_text(
                "🔷 تم قبول غيابكِ بعذر وتحديث المحضر تلقائياً. جزاكِ الله خيراً."
            )
            try:
                await application.bot.send_message(
                    ADMIN_ID, f"📋 الطالبة {student_clean} قدّمت عذراً وتم تحديث المحضر."
                )
            except Exception:
                pass

        # ── رفض العذر ──
        elif data.startswith("exc_n_"):
            rest = data[6:]
            dk, student_clean = rest[:8], rest[9:]
            key = clean_username(student_clean)
            unexcused_absences[key] = unexcused_absences.get(key, 0) + 1
            save_json("unexcused.json", unexcused_absences)
            count = unexcused_absences[key]
            if count < MAX_UNEXCUSED:
                await query.message.edit_text(
                    f"⚠️ تنبيه هام:\n\n"
                    f"تم تسجيل غيابكِ بغير عذر عن حلقة اليوم.\n"
                    f"عدد غياباتكِ بغير عذر: {count}/{MAX_UNEXCUSED}\n\n"
                    "نذكّركِ بضرورة الحرص على الحضور والمثابرة. وفقكِ الله."
                )
                try:
                    await application.bot.send_message(
                        ADMIN_ID,
                        f"⚠️ الطالبة {student_clean} غائبة بلا عذر ({count}/{MAX_UNEXCUSED})."
                    )
                except Exception:
                    pass
            else:
                await query.message.edit_text(
                    "🚫 إشعار رسمي بالإقصاء التلقائي:\n\n"
                    "نظراً لتجاوزكِ حد الـ 3 غيابات بدون عذر، فقد تم تجميد مقعدكِ بالمقرأ تلقائياً.\n"
                    "للاستفسار تواصلي مع الإدارة."
                )
                try:
                    await application.bot.send_message(
                        ADMIN_ID,
                        f"🚫 إقصاء تلقائي للطالبة {student_clean} ({MAX_UNEXCUSED} غيابات بلا عذر)."
                    )
                except Exception:
                    pass

        # ── الأسئلة الشائعة ──
        elif data.startswith("faq_"):
            if data == "faq_custom":
                inquiry_pending.add(norm_user)
                try:
                    await query.message.reply_text(
                        "📝 تفضلي بكتابة استفساركِ هنا وسنردّ عليكِ قريباً إن شاء الله:"
                    )
                except Exception:
                    pass
                await query.answer()

            elif data == "faq_add_new" and is_staff:
                faq_add_pending.add(user_id)
                await application.bot.send_message(
                    user_id,
                    "📝 اكتبي السؤال في السطر الأول والجواب في السطر الثاني:\n\n"
                    "مثال:\nما هي أوقات الحصة؟\nالحصة كل أسبوع يوم السبت الساعة 8 مساءً."
                )
                await query.answer()

            elif data.startswith("faq_edit_") and is_staff:
                try:
                    faq_id = int(data[9:])
                    faq_edit_pending[user_id] = faq_id
                    for item in faq_items:
                        if item["id"] == faq_id:
                            await application.bot.send_message(
                                user_id,
                                f"✏️ تعديل السؤال:\n\n"
                                f"السؤال الحالي: {item['q']}\n"
                                f"الجواب الحالي: {item['a']}\n\n"
                                "📝 أرسلي السؤال الجديد في السطر الأول والجواب في السطر الثاني.\n"
                                "أو اكتبي «إلغاء» للتراجع."
                            )
                            await query.answer()
                            return
                    await query.answer("❌ السؤال غير موجود.")
                except (ValueError, IndexError):
                    await query.answer()

            else:
                try:
                    fid = int(data[4:])
                    for item in faq_items:
                        if item["id"] == fid:
                            await query.answer(item["a"][:200], show_alert=True)
                            return
                    await query.answer()
                except (ValueError, Exception):
                    await query.answer()

        else:
            try:
                await query.answer()
            except Exception:
                pass

    except Exception as e:
        logger.exception(f"Error in handle_callbacks: {e}")
        try:
            await query.answer()
        except Exception:
            pass

# ══════════════════════════════════════════════════
#              تشغيل البوت
# ══════════════════════════════════════════════════
async def main():
    global application
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (
            filters.TEXT | filters.COMMAND |
            filters.PHOTO | filters.Document.ALL |
            filters.VIDEO | filters.AUDIO | filters.VOICE |
            filters.Sticker.ALL | filters.ANIMATION
        ),
        handle_private
    ))
    application.add_handler(CallbackQueryHandler(handle_callbacks))

    scheduler.start()
    # استعادة الجدولة المحفوظة عند إعادة تشغيل البوت
    apply_scheduled_jobs()

    async with application:
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        logger.info("🌴 مقرأ الإمام نافع — البوت يعمل الآن بكفاءة!")
        await asyncio.sleep(float("inf"))
        await application.updater.stop()
        await application.stop()

if __name__ == "__main__":
    keep_alive()
    asyncio.run(main())
