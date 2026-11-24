# peyamesh — SDK رسمی پایتون درگاه پرداخت پیامش

[![PyPI](https://img.shields.io/pypi/v/peyamesh.svg)](https://pypi.org/project/peyamesh/)
[![Python](https://img.shields.io/pypi/pyversions/peyamesh.svg)](https://pypi.org/project/peyamesh/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

اتصال هر پروژه‌ی پایتونی به درگاه پرداخت اینترنتی **پیامش**، در چند خط:

- **کلاینت مستقل** (sync و async) برای هر پروژه‌ی پایتونی
- **یکپارچه‌سازی آماده‌ی جنگو**: اضافه کردن به `INSTALLED_APPS` و تعریف `PEYAMESH` در `settings.py`. مدل، پنل ادمین، صفحه‌ی بازگشت، وبهوک، signal و دستور `peyamesh_reconcile` آماده‌اند.
- **یکپارچه‌سازی آماده‌ی FastAPI**: یک `include_router` به‌همراه hookها و ذخیره‌سازی با SQLAlchemy (تست‌شده روی PostgreSQL و SQLite)

همه‌ی کارهای حساس پرداخت را هم خود پکیج انجام می‌دهد:

- verify اجباری با مبلغ ذخیره‌شده در دیتابیس خودتان (پارامتر `Status=OK` آدرس بازگشت هرگز ملاک نیست)
- hook تحویل سفارش برای هر پرداخت **یک بار** اجرا می‌شود، حتی وقتی بازگشت خریدار، وبهوک و reconcile هم‌زمان برسند. اگر hook خطا بدهد یا پردازه وسط کار از کار بیفتد، reconcile آن را **دوباره اجرا می‌کند تا موفق شود**. پس هیچ سفارش پرداخت‌شده‌ای بی‌صدا جا نمی‌ماند.
- پرداخت‌هایی که خریدار بعد از پرداخت مرورگر را بسته، با وبهوک و reconcile تأیید می‌شوند
- امضای وبهوک‌ها بررسی می‌شود (HMAC-SHA256 با مقایسه‌ی امن، رد امضای قدیمی، پشتیبانی از چرخش کلید) و رویدادهای تکراری نادیده گرفته می‌شوند
- `Idempotency-Key` خودکار و retry امن برای خطای شبکه، timeout، `429` و `5xx`
- خطاها نوع مشخص دارند و `code` ماشینی و پیام فارسی قابل نمایش به خریدار دارند
- ریال و تومان یک بار و فقط در مرز تبدیل می‌شوند
- `FakeGateway` برای تست کامل فرایند پرداخت، بدون شبکه و بدون کلید API

> نیازمندی‌ها: Python 3.9 به بالا؛ Django 4.2 به بالا؛ FastAPI 0.100 و SQLAlchemy 2.0 به بالا.

---

## فهرست

- [نصب](#نصب)
- [مفاهیم پایه](#مفاهیم-پایه)
- [جنگو](#جنگو)
- [FastAPI](#fastapi)
- [استفاده‌ی مستقیم از SDK](#استفاده‌ی-مستقیم-از-sdk)
- [وضعیت‌های پرداخت](#وضعیتهای-پرداخت)
- [تست بدون شبکه با FakeGateway](#تست-بدون-شبکه-با-fakegateway)
- [نکات امنیتی و عملیاتی](#نکات-امنیتی-و-عملیاتی)
- [توسعه‌ی پکیج](#توسعهی-پکیج)

---

## نصب

```bash
pip install peyamesh            # فقط کلاینت
pip install "peyamesh[django]"  # + یکپارچه‌سازی جنگو
pip install "peyamesh[fastapi]" # + یکپارچه‌سازی FastAPI (همراه SQLAlchemy)
```

برای FastAPI یک درایور async دیتابیس هم لازم است، مثلاً `asyncpg` برای PostgreSQL یا `aiosqlite` برای SQLite.

## مفاهیم پایه

1. **کلید API** را از بخش «توسعه‌دهندگان» پنل پذیرنده بسازید. کلید **آزمایشی** پرداخت‌ها را روی درگاه آزمایشی انجام می‌دهد و پول واقعی جابه‌جا نمی‌شود. کلید **عملیاتی** برای پرداخت واقعی است.
2. **جریان پرداخت:** ابتدا پرداخت را ایجاد می‌کنید و خریدار را به `payment_url` می‌فرستید. بعد از بانک، خریدار با `?Authority=...&Status=OK|NOK` به `callback_url` برمی‌گردد. شما پرداخت را **verify** می‌کنید و بعد سفارش را تحویل می‌دهید.
3. **وبهوک:** در همان بخش «توسعه‌دهندگان» یک وبهوک تعریف کنید و کلید امضای آن (`whsec_...`) را در تنظیمات بگذارید. با وبهوک، نتیجه‌ی پرداخت حتی وقتی خریدار به سایت برنگردد به دستتان می‌رسد.
4. **واحد پول:** مبالغ را در واحدی می‌دهید که در تنظیمات (`IRT` یعنی تومان یا `IRR` یعنی ریال) یا در هر فراخوانی مشخص کرده‌اید. فیلدهایی که با `_irr` تمام می‌شوند همیشه ریال‌اند.

---

## جنگو

### ۱. نصب و تنظیمات

```python
# settings.py
INSTALLED_APPS = [
    # ...
    "peyamesh.contrib.django",
]

PEYAMESH = {
    "API_KEY": env("PEYAMESH_API_KEY"),
    "CURRENCY": "IRT",                         # مبالغی که می‌دهید به تومان است
    "WEBHOOK_SECRET": env("PEYAMESH_WEBHOOK_SECRET"),
    "SUCCESS_URL": "orders:payment-success",   # نام url، مسیر یا آدرس کامل
    "FAILURE_URL": "orders:payment-failed",
}
```

```python
# urls.py
urlpatterns = [
    # ...
    path("peyamesh/", include("peyamesh.contrib.django.urls")),
]
```

```bash
python manage.py migrate
python manage.py check      # تنظیمات ناقص یا غلط را گزارش می‌دهد
```

با این include دو آدرس ساخته می‌شود:

| آدرس | کاربرد |
|---|---|
| `/peyamesh/callback/` | بازگشت خریدار از درگاه. verify انجام می‌شود و خریدار به صفحه‌ی نتیجه هدایت می‌شود. |
| `/peyamesh/webhook/` | این آدرس کامل را (مثلاً `https://shop.ir/peyamesh/webhook/`) در پنل به‌عنوان وبهوک ثبت کنید. |

### ۲. شروع پرداخت

```python
from peyamesh import PeyameshError
from peyamesh.contrib.django import start_payment

def checkout(request, order_id):
    order = get_object_or_404(Order, pk=order_id, user=request.user)
    try:
        payment = start_payment(
            request,
            amount=order.total,          # به واحد PEYAMESH["CURRENCY"]
            order_id=order.pk,
            description=f"سفارش {order.pk}",
            mobile=order.phone,          # اختیاری
        )
    except PeyameshError as exc:
        messages.error(request, exc.message)   # پیام فارسی قابل نمایش
        return redirect("cart")
    return payment.redirect()
```

`start_payment` یک رکورد `PeyameshPayment` در دیتابیس شما می‌سازد، پرداخت را در درگاه ثبت می‌کند و رکورد را برمی‌گرداند. اگر درگاه درخواست را رد کند، رکورد با وضعیت `error` و کد خطا باقی می‌ماند.

### ۳. تحویل سفارش (signal)

```python
# orders/receivers.py  (در AppConfig.ready ایمپورت شود)
from django.dispatch import receiver
from peyamesh.contrib.django.signals import payment_verified, payment_failed

@receiver(payment_verified)
def deliver(sender, payment, **kwargs):
    # فیلتر status اجرای دوباره را بی‌اثر می‌کند
    if Order.objects.filter(pk=payment.order_id, status="unpaid").update(status="paid", paid_ref=payment.ref_id):
        send_receipt_sms(payment.mobile)

@receiver(payment_failed)
def release_stock(sender, payment, **kwargs):
    ...
```

`payment_verified` برای هر پرداخت یک بار و **بعد از commit تراکنش دیتابیس** ارسال می‌شود. خطای receiver لاگ می‌شود (logger با نام `peyamesh.django`) ولی ریدایرکت خریدار و پاسخ وبهوک را خراب نمی‌کند.

**تضمین تحویل سفارش:** اگر یکی از receiverهای `payment_verified` خطا بدهد، یا پردازه درست بعد از تأیید پرداخت از کار بیفتد، `peyamesh_reconcile` این signal را دوباره به **همه‌ی** receiverها می‌فرستد تا همه بدون خطا اجرا شوند (حداکثر تا ۳ روز بعد از تأیید). زمان اجرای موفق در فیلد `notified_at` ثبت می‌شود و `PeyameshPayment.objects.unnotified()` پرداخت‌هایی را که هنوز منتظرند برمی‌گرداند. پس receiver را طوری بنویسید که اجرای دوباره‌اش بی‌ضرر باشد، مثل نمونه‌ی بالا.

| Signal | آرگومان‌ها | زمان ارسال |
|---|---|---|
| `payment_verified` | `payment` | پرداخت تأیید شد. سفارش را تحویل دهید. |
| `payment_failed` | `payment` | پرداخت ناموفق بود، لغو شد یا منقضی شد. |
| `payment_refunded` | `payment`, `refund` | مبلغی بازپرداخت شد. |
| `webhook_received` | `event`, `payment` | هر رویداد وبهوکِ معتبر بعد از پردازش. |

### ۴. صفحه‌ی نتیجه

خریدار به `SUCCESS_URL` یا `FAILURE_URL` هدایت می‌شود و این پارامترها به آدرس اضافه می‌شوند: `?payment=<uuid>&status=success|failed|pending`. تصمیم را همیشه از دیتابیس بگیرید، نه از پارامتر `status`:

```python
from peyamesh.contrib.django import get_returned_payment

def payment_result(request):
    payment = get_returned_payment(request)
    if payment and payment.is_paid:
        return render(request, "orders/success.html", {"payment": payment})
    return render(request, "orders/failed.html", {"payment": payment})
```

برای فرستادن خریدار به صفحه‌ای مخصوص یک پرداخت، `start_payment(..., return_url="/orders/42/")` را بدهید.

`status=pending` یعنی نتیجه‌ی بانک هنوز قطعی نشده است. این پرداخت خودکار (با وبهوک یا reconcile) تعیین تکلیف می‌شود و signal مربوط ارسال می‌شود.

### ۵. Reconcile دوره‌ای (حتماً زمان‌بندی کنید)

```bash
python manage.py peyamesh_reconcile          # هر ۵ تا ۱۰ دقیقه (cron یا systemd timer)
```

این دستور چهار کار انجام می‌دهد:

- پرداخت‌هایی را که پول‌شان دریافت شده ولی verify نشده‌اند تأیید می‌کند (مثلاً خریدار بلافاصله بعد از پرداخت مرورگر را بسته).
- وضعیت پرداخت‌های `pending` قدیمی را دوباره بررسی می‌کند.
- `payment_verified` را برای پرداخت‌هایی که receiverشان خطا داده بود دوباره می‌فرستد.
- رویدادهای وبهوکِ قدیمی‌تر از `WEBHOOK_EVENT_RETENTION_DAYS` را پاک می‌کند تا جدول بی‌انتها بزرگ نشود.

با Celery هم می‌شود:

```python
from celery import shared_task
from peyamesh.contrib.django import reconcile

@shared_task
def peyamesh_reconcile():
    return reconcile()
```

### ۶. بازپرداخت

```python
from peyamesh.contrib.django import refund_payment

refund_payment(payment, amount=50_000, reason="مرجوعی یک قلم")   # جزئی (به واحد پرداخت)
refund_payment(payment)                                           # کل مبلغ باقی‌مانده
```

اگر بازپرداخت نیاز به تأیید داشته باشد، وضعیت رکورد بعد از تکمیل و با وبهوک `payment.refunded` به‌روز می‌شود. برای اینکه کلیک تکراری بازپرداخت دوباره نسازد، `idempotency_key` بدهید (مثلاً شناسه‌ی درخواست مرجوعی).

### ۷. پنل ادمین

بخش «پرداخت‌های پیامش» فقط‌خواندنی است، تا سابقه‌ی مالی دستکاری نشود. جستجو بر اساس Authority، شماره سفارش، شماره پیگیری، موبایل و کارت امکان دارد. اکشن «همگام‌سازی با درگاه» وضعیت پرداخت‌های انتخاب‌شده را از درگاه می‌گیرد. با فیلتر «اعلام تأیید به فروشگاه» می‌توانید پرداخت‌های موفقی را ببینید که receiverشان هنوز موفق نشده است.

### ۸. همه‌ی تنظیمات `PEYAMESH`

| کلید | پیش‌فرض | توضیح |
|---|---|---|
| `API_KEY` | متغیر محیطی `PEYAMESH_API_KEY` | **الزامی** |
| `CURRENCY` | واحد پیش‌فرض فروشگاه | `"IRT"` یا `"IRR"` (توصیه: صریح تعیین کنید) |
| `WEBHOOK_SECRET` | متغیر محیطی `PEYAMESH_WEBHOOK_SECRET` | رشته یا لیست (برای چرخش کلید) |
| `WEBHOOK_TOLERANCE` | `300` | حداکثر عمر امضا بر حسب ثانیه؛ `None` = بدون بررسی |
| `SUCCESS_URL` / `FAILURE_URL` | `"/"` | نام url، مسیر یا آدرس کامل |
| `PENDING_URL` | `FAILURE_URL` | صفحه‌ی حالت «در حال بررسی» |
| `CALLBACK_URL` | ساخته از `request` | آدرس کامل بازگشت؛ پشت proxy یا CDN مفید است |
| `SITE_URL` | — | مثل `"https://shop.ir"`؛ برای `start_payment` بدون `request` |
| `RETURN_QUERY_PARAM` | `"payment"` | نام پارامتر شناسه در آدرس نتیجه |
| `TIMEOUT` / `MAX_RETRIES` | `20` / `2` | تنظیمات ارتباط با API |
| `RECONCILE_AFTER_MINUTES` / `RECONCILE_LIMIT` | `15` / `200` | پرداخت‌های pending (و receiverهای ناموفق) قدیمی‌تر از این، دوباره بررسی می‌شوند |
| `WEBHOOK_EVENT_RETENTION_DAYS` | `30` | رویدادهای وبهوک پردازش‌شده تا این تعداد روز برای تشخیص ارسال تکراری نگه داشته می‌شوند؛ `None` = برای همیشه |
| `BASE_URL` | `https://peyamesh.ir/api/v1` | فقط برای محیط‌های خاص |

---

## FastAPI

### ۱. راه‌اندازی

```python
# main.py
from fastapi import FastAPI, Request
from peyamesh import PeyameshError
from peyamesh.contrib.fastapi import PeyameshFastAPI, PeyameshSettings, SQLAlchemyStore

peyamesh = PeyameshFastAPI(
    PeyameshSettings.from_env(          # PEYAMESH_API_KEY, PEYAMESH_WEBHOOK_SECRET, ...
        currency="IRT",
        success_url="/orders/success",
        failure_url="/orders/failed",
    ),
    store=SQLAlchemyStore("postgresql+asyncpg://user:pass@localhost/shop"),
)

app = FastAPI(lifespan=peyamesh.lifespan)
app.include_router(peyamesh.router, prefix="/peyamesh")
```

با این router دو آدرس ساخته می‌شود: `/peyamesh/callback` و `/peyamesh/webhook`. آدرس کامل وبهوک را در پنل ثبت کنید. جدول‌ها هنگام شروع برنامه ساخته می‌شوند (`peyamesh_payments` و `peyamesh_webhook_events`).

اگر lifespan خودتان را دارید:

```python
@asynccontextmanager
async def lifespan(app):
    async with peyamesh.lifespan(app):
        yield
```

### ۲. شروع پرداخت و صفحه‌ی نتیجه

```python
@app.post("/checkout/{order_id}")
async def checkout(order_id: int, request: Request):
    order = await orders.get(order_id)
    try:
        payment = await peyamesh.start_payment(request, amount=order.total, order_id=order_id)
    except PeyameshError as exc:
        return templates.TemplateResponse(request, "cart.html", {"error": exc.message})
    return payment.redirect()          # 303 به صفحه‌ی پرداخت


@app.get("/orders/success")
@app.get("/orders/failed")
async def result(request: Request):
    payment = await peyamesh.get_returned_payment(request)
    return {"paid": bool(payment and payment.is_paid)}
```

### ۳. Hookها

```python
@peyamesh.on_payment_verified
async def deliver(payment):              # اگر خطا بدهد، reconcile دوباره اجرایش می‌کند؛ پس idempotent بنویسید
    await orders.mark_paid_if_unpaid(payment.order_id, ref_id=payment.ref_id)

@peyamesh.on_payment_failed
def notify(payment):                     # تابع sync هم پشتیبانی می‌شود (در threadpool اجرا می‌شود)
    ...

@peyamesh.on_payment_refunded
async def refunded(payment, refund): ...

@peyamesh.on_webhook_event
async def any_event(event, payment): ...
```

`on_payment_verified` همان تضمین جنگو را دارد: اگر یکی از hookها خطا بدهد، `reconcile` همه‌ی آن‌ها را دوباره اجرا می‌کند تا موفق شوند (حداکثر تا ۳ روز). زمان اجرای موفق در `payment.notified_at` ثبت می‌شود.

### ۴. Reconcile و بازپرداخت

```python
# در پس‌زمینه‌ی برنامه
@asynccontextmanager
async def lifespan(app):
    async with peyamesh.lifespan(app):
        task = asyncio.create_task(peyamesh.run_reconciler(interval=300))
        yield
        task.cancel()

# یا از cron یا worker:  await peyamesh.reconcile()

await peyamesh.refund(payment, amount=50_000, reason="مرجوعی")
await peyamesh.refund(payment)   # کل مبلغ باقی‌مانده
```

> وقتی برنامه را با چند worker اجرا می‌کنید، `run_reconciler` را فقط در یک پردازه اجرا کنید. اجرای هم‌زمان به داده آسیبی نمی‌زند، فقط درخواست اضافه می‌فرستد.

### ۵. ذخیره‌سازی

| Store | کاربرد |
|---|---|
| `SQLAlchemyStore(url_or_async_engine)` | production. فقط از SQLAlchemy Core استفاده می‌کند و روی PostgreSQL و SQLite تست شده است. |
| `SQLAlchemyStore(engine, metadata=Base.metadata, create_tables=False)` | جدول‌ها را با Alembic در migrationهای خودتان مدیریت کنید. |
| `MemoryStore()` | فقط برای تست و دمو (داده‌ها با ری‌استارت از بین می‌روند). |
| پیاده‌سازی `PaymentStore` | هر ذخیره‌ساز دیگری. تنها شرط این است که `transition` و `claim` عملیات compare-and-set اتمیک باشند. |

### ۶. تنظیمات (`PeyameshSettings`)

همه‌ی فیلدها را می‌شود از متغیرهای محیطی با پیشوند `PEYAMESH_` خواند: `PEYAMESH_API_KEY`، `PEYAMESH_CURRENCY`، `PEYAMESH_WEBHOOK_SECRET` (چند مقدار با کاما جدا می‌شوند)، `PEYAMESH_SUCCESS_URL`، `PEYAMESH_FAILURE_URL`، `PEYAMESH_PENDING_URL`، `PEYAMESH_CALLBACK_URL`، `PEYAMESH_TIMEOUT`، `PEYAMESH_MAX_RETRIES`، `PEYAMESH_WEBHOOK_TOLERANCE`، `PEYAMESH_RECONCILE_AFTER_MINUTES`، `PEYAMESH_RECONCILE_LIMIT` و `PEYAMESH_WEBHOOK_EVENT_RETENTION_DAYS` (پیش‌فرض ۳۰؛ `none` = برای همیشه).

> پشت nginx یا CDN: یا uvicorn را با `--proxy-headers` اجرا کنید یا `callback_url="https://shop.ir/peyamesh/callback"` را صریح بدهید، تا آدرس بازگشت با `https` و دامنه‌ی درست ساخته شود.

---

## استفاده‌ی مستقیم از SDK

برای Flask، اسکریپت‌ها، worker یا هر فریمورک دیگری:

```python
from peyamesh import Peyamesh, PaymentNotPaidError

client = Peyamesh(api_key="...", currency="IRT")   # یا متغیر محیطی PEYAMESH_API_KEY

payment = client.create_payment(
    245_000, "https://shop.ir/pay/back",
    order_id="1048", description="سفارش ۱۰۴۸", mobile="09120000000",
)
save(order_id="1048", authority=payment.authority, amount=245_000)
# خریدار را به payment.payment_url بفرستید

# در آدرس بازگشت:
order = load_by_authority(request.args["Authority"])
try:
    result = client.verify_payment(order.authority, order.amount)   # مبلغ از دیتابیس خودتان
except PaymentNotPaidError as exc:
    ...  # exc.payment_status: failed | expired | unknown | ...
else:
    deliver(order, ref_id=result.ref_id)    # result.code: 100 = همین الان، 101 = قبلاً تأیید شده
```

نسخه‌ی async همین API را دارد:

```python
from peyamesh import AsyncPeyamesh

async with AsyncPeyamesh(currency="IRT") as client:
    payment = await client.create_payment(245_000, "https://shop.ir/pay/back")
```

| متد | توضیح |
|---|---|
| `create_payment(amount, callback_url, *, currency, description, order_id, mobile, email, metadata, idempotency_key)` | ایجاد پرداخت و دریافت `payment_url` |
| `verify_payment(authority, amount, *, currency)` | تأیید پرداخت (کد `100` یا `101`) |
| `verify_callback(params, amount)` | خواندن `Authority` از پارامترهای بازگشت به‌همراه verify |
| `get_payment(authority)` | استعلام وضعیت |
| `list_unverified_payments()` | پرداخت‌هایی که پول‌شان دریافت شده ولی verify نشده‌اند |
| `create_refund(authority, *, amount, currency, reason, destination_iban, destination_card, idempotency_key)` | بازپرداخت جزئی یا کامل |
| `get_refund(refund_id)` | وضعیت بازپرداخت |

### خطاها

```python
from peyamesh import (PeyameshError, InvalidRequestError, AuthenticationError, RateLimitError,
                      APIConnectionError, APITimeoutError, ServiceUnavailableError)

try:
    client.create_payment(...)
except InvalidRequestError as exc:       # مثلاً amount_too_low، validation_error، callback_domain_mismatch
    print(exc.code, exc.message, exc.details, exc.request_id)
except APITimeoutError:                  # نتیجه نامعلوم است؛ با همان idempotency_key دوباره تلاش کنید
    ...
```

`PeyameshError` ← `APIError` ← (`InvalidRequestError` شامل `AmountMismatchError` و `PaymentNotPaidError`)، `AuthenticationError`، `PermissionDeniedError`، `NotFoundError`، `ConflictError`، `RateLimitError`، `ServiceUnavailableError` و `ServerError`. به‌علاوه `APIConnectionError` (و زیرکلاس آن `APITimeoutError`)، `SignatureVerificationError` و `ConfigurationError`.

### Retry و idempotency

- خطای شبکه، timeout، `429` (با رعایت `retry_after`)، `500`، `502`، `504` و `503 payment_busy` به‌صورت خودکار تا `max_retries` بار (پیش‌فرض ۲) با backoff نمایی تکرار می‌شوند. `503 no_gateway_available` تکرار نمی‌شود، چون نتیجه‌اش عوض نمی‌شود.
- ایجاد پرداخت و بازپرداخت همیشه `Idempotency-Key` دارند. پس تکرار درخواست هیچ‌وقت پرداخت دوم نمی‌سازد. اگر بین چند پردازه دوباره تلاش می‌کنید، `idempotency_key` خودتان را بدهید.
- `X-Request-ID` در همه‌ی تلاش‌های یک درخواست یکسان است و در `exc.request_id` در دسترس است؛ برای پیگیری با پشتیبانی به کار می‌آید.

### وبهوک بدون فریمورک

```python
from peyamesh import webhooks, SignatureVerificationError

try:
    event = webhooks.construct_event(
        payload=raw_body_bytes,                              # بدنه‌ی خام، قبل از parse
        signature_header=headers.get("Peyamesh-Signature"),
        secret=["whsec_new", "whsec_old"],                   # چرخش کلید
    )
except SignatureVerificationError:
    return 400
if event.type == "payment.succeeded":
    client.verify_payment(event.payment.authority, amount_from_your_db)
```

رویدادها: `payment.created`، `payment.pending`، `payment.succeeded` (پرداخت شده و منتظر verify)، `payment.failed`، `payment.expired`، `payment.refunded`، `refund.failed`، `settlement.completed` و `settlement.failed`. شناسه‌ی `event.id` در تلاش‌های مجدد ثابت است. از آن برای حذف رویداد تکراری استفاده کنید. پاسخ `2xx` یعنی رویداد دریافت شد. هر پاسخ دیگری باعث ارسال دوباره می‌شود.

---

## وضعیت‌های پرداخت

وضعیت رکورد محلی (`payment.status` در جنگو و FastAPI):

| وضعیت | معنی |
|---|---|
| `new` | رکورد ساخته شده و ثبت در درگاه در جریان است |
| `pending` | منتظر پرداخت خریدار یا قطعی شدن نتیجه‌ی بانک |
| `verified` | **پرداخت موفق و تأییدشده.** سفارش را تحویل دهید (`is_paid`). |
| `failed` | ناموفق، لغوشده یا منقضی‌شده |
| `error` | درگاه درخواست ایجاد پرداخت را رد کرد (`failure_code`) |
| `partially_refunded` / `refunded` | بازپرداخت جزئی یا کامل (`is_paid` همچنان `True`) |

یک پرداخت `failed` اگر بعداً تأیید بانکی‌اش برسد، به `verified` تغییر می‌کند و signal یا hook مربوط اجرا می‌شود. منطق تحویل را طوری بنویسید که این حالت را هم پوشش دهد.

### پرداخت دوباره‌ی یک سفارش

اگر خریدار صفحه‌ی پرداخت را در دو تب باز کند و هر دو را بپردازد، برای یک سفارش دو پرداخت موفق دارید. برنده را یک `UPDATE` شرطی روی جدول سفارش خودتان تعیین کند و پرداخت دیگر را برگردانید:

```python
@receiver(payment_verified)
def deliver(sender, payment, **kwargs):
    if Order.objects.filter(pk=payment.order_id, status="unpaid").update(status="paid", paid_ref=payment.ref_id):
        return                                     # همین پرداخت سفارش را پرداخت کرد
    paid_by_another = Order.objects.filter(pk=payment.order_id, status="paid").exclude(paid_ref=payment.ref_id)
    if paid_by_another.exists() and payment.refundable_irr:
        refund_payment(payment, reason="پرداخت تکراری", idempotency_key=f"dup-{payment.public_id}")
```

با `idempotency_key` ثابت، اجرای دوباره‌ی hook بازپرداخت دوم نمی‌سازد. در FastAPI همین منطق را با `await peyamesh.refund(...)` بنویسید. فهرست همه‌ی پرداخت‌های یک سفارش هم با `PeyameshPayment.objects.for_order(order_id)` (جنگو) یا `await peyamesh.get_payments_for_order(order_id)` (FastAPI) در دسترس است.

---

## تست بدون شبکه با FakeGateway

`peyamesh.testing.FakeGateway` درگاهی شبیه‌سازی‌شده در حافظه است. کدهای خطا، idempotency، تبدیل ریال و تومان، بازپرداخت‌های در جریان و امضای وبهوک‌هایش مثل سرویس واقعی است.

```python
from peyamesh.testing import FakeGateway
from peyamesh.contrib.django import override_client

def test_checkout(client):
    gateway = FakeGateway()
    with override_client(gateway.client(currency="IRT")):
        response = client.post("/checkout/42/")
        payment = PeyameshPayment.objects.get(order_id="42")

        gateway.pay(payment.authority)                     # خریدار در بانک پرداخت کرد
        client.get(gateway.callback_url(payment.authority).replace("http://testserver", ""))
        payment.refresh_from_db()
        assert payment.is_paid

        body, headers = gateway.webhook("payment.succeeded", payment.authority)   # وبهوک امضاشده
```

برای FastAPI: `PeyameshFastAPI(settings, store=MemoryStore(), client=gateway.async_client())`.

امکانات دیگر: `gateway.fail()`، `expire()`، `mark_unknown()`، `fail_next(status, code)` (تزریق خطای API)، `network_error_next(timeout=True, after_processing=True)` و `FakeGateway(auto_complete_refunds=False)` به‌همراه `complete_refund()`.

> signalهای جنگو بعد از commit ارسال می‌شوند. در `TestCase` از `self.captureOnCommitCallbacks(execute=True)` و در pytest-django از `@pytest.mark.django_db(transaction=True)` استفاده کنید.

---

## نکات امنیتی و عملیاتی

- کلید API و کلید وبهوک را در متغیر محیطی یا secret manager نگه دارید، نه در کد.
- verify را همیشه با مبلغ ذخیره‌شده‌ی خودتان انجام دهید. پکیج همین کار را می‌کند و `Status` آدرس بازگشت را نادیده می‌گیرد.
- وبهوک و reconcile را راه‌اندازی کنید. بدون آن‌ها پرداخت خریدارانی که به سایت برنمی‌گردند فقط با دستور reconcile تأیید می‌شود.
- اگر در پنل برای کلید API محدودیت IP تعریف کرده‌اید، IP خروجی سرورتان را اضافه کنید.
- لاگ‌ها با loggerهای `peyamesh`، `peyamesh.django` و `peyamesh.fastapi` نوشته می‌شوند. کلید API هرگز لاگ نمی‌شود.

---

## توسعه‌ی پکیج

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e . && pip install --group dev        # pip 25.1 به بالا؛ یا: pip install pytest pytest-django anyio aiosqlite ...
pytest                                              # SQLite
PEYAMESH_TEST_POSTGRES=postgres://user:pass@localhost:5432/db pytest   # روی PostgreSQL (به psycopg و asyncpg نیاز دارد)
ruff check src tests && ruff format --check src tests && mypy
```

تاریخچه‌ی تغییرات در [CHANGELOG.md](CHANGELOG.md) است.

## مجوز

MIT
