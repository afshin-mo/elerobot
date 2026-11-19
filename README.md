# Elevator Bot

ربات تلگرام مدیریت خرابی آسانسور (Python + aiogram + PostgreSQL). مرحله‌ی فعلی: ۱ (پایه).

## نصب برای توسعه (بدون Docker)

۱. PostgreSQL 16 را روی کامپیوتر نصب کن و دو دیتابیس بساز:

```sql
CREATE USER elevator WITH PASSWORD 'رمز-دلخواه' CREATEDB;
CREATE DATABASE elevator_dev OWNER elevator;
CREATE DATABASE elevator_test OWNER elevator;
```

۲. محیط پایتون (نسخه ۳.۱۱ یا بالاتر):

```bash
python -m venv .venv
source .venv/bin/activate        # ویندوز: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

۳. فایل `.env.example` را به `.env` کپی کن و مقدارها را پر کن (`BOT_TOKEN` را از BotFather بگیر).

۴. ساخت جدول‌ها و اجرای تست‌ها:

```bash
alembic upgrade head
pytest
python -m app.main               # اجرای ربات (Long Polling)؛ با Ctrl+C متوقف می‌شود
```

**نکته‌ی ویندوز:** فایل `.env` را با رمزگذاری UTF-8 ذخیره کن (چون `COMPANY_NAME` فارسی است). مقدار `INITIAL_MANAGER_TELEGRAM_ID` باید شناسه‌ی عددی تلگرام اولین مدیر باشد. اولین مدیر با همین حساب `/start` می‌زند، زبان را انتخاب می‌کند و شماره‌اش را می‌فرستد.

**بعد از هر به‌روزرسانی** ابتدا `alembic upgrade head` را بزن (مرحله‌ی ۳ یک migration جدید دارد).

**هشدار:** تست‌ها دیتابیس `TEST_DATABASE_URL` را کاملاً پاک و از نو می‌سازند؛ هرگز آن را به دیتابیس واقعی وصل نکن.

## دستورهای مفید

- migration جدید بعد از تغییر مدل‌ها: `alembic revision --autogenerate -m "توضیح"` و بررسی دستی فایل ساخته‌شده.
- برگشت یک مرحله: `alembic downgrade -1`

## ساختار

`app/models` مدل‌ها، `app/core` تنظیمات/لاگ/دیتابیس، `migrations` تغییرات دیتابیس، `tests` تست‌ها، `docs` تصمیمات و کارها.
فایل‌های ترجمه در `app/i18n` هستند. متن‌های سورانی پیش‌نویس‌اند و باید توسط مالک کنترل شوند.
