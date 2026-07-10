# تشغيل سكربت الإعلان على Railway لمدة 24 ساعة

## الملفات
- `telegram_promo.py`: السكربت الأساسي.
- `generate_session.py`: تسجيل الدخول مرة واحدة واستخراج جلسة سرية.
- `requirements.txt`: المكتبات المطلوبة.

## 1) جهّز الرسالة
افتح Telegram ثم أرسل الرسالة الترويجية كاملة إلى **Saved Messages**.
لا ترسل بعدها رسالة أخرى إلى Saved Messages لأن السكربت يأخذ آخر رسالة تلقائيًا.

## 2) هات API_ID وAPI_HASH
افتح `my.telegram.org`، وسجّل دخولك، ثم افتح **API development tools** وأنشئ تطبيقًا.
احتفظ بقيمتي `api_id` و`api_hash`.

## 3) استخرج TG_SESSION
على Termux:

```bash
pkg update
pkg install python
pip install telethon==1.44.0
python generate_session.py
```

اكتب رقم الهاتف وكود Telegram عند الطلب. سيظهر نص طويل؛ انسخه كاملًا، وهو قيمة `TG_SESSION`.

## 4) ارفع الملفات إلى GitHub
أنشئ Repository خاصًا، وارفع الملفات الثلاثة إليه.
لا ترفع قيمة `TG_SESSION` أو أي ملف جلسة إلى GitHub.

## 5) أنشئ خدمة Railway
- اختر **New Project**
- اختر **Deploy from GitHub repo**
- اختر الـRepository
- من **Settings** اجعل Start Command:

```text
python telegram_promo.py
```

## 6) أضف Variables في Railway

```text
TG_API_ID=رقم API ID
TG_API_HASH=قيمة API HASH
TG_SESSION=النص الطويل الناتج من generate_session.py
TG_TARGETS=ChatgptPlusDeal,clashers_market_1
PROMO_INTERVAL_SECONDS=480
```

`480` ثانية تعني 8 دقائق.

## 7) التشغيل
اعمل Deploy ثم افتح Logs. المفروض يظهر:
- Logged in as ...
- Sent successfully to @ChatgptPlusDeal
- Sent successfully to @clashers_market_1

## مهم
قيمة `TG_SESSION` سرية جدًا وتعطي إمكانية الدخول إلى حساب Telegram. لا ترسلها لأي شخص ولا تضعها داخل GitHub.
تأكد أن النشر كل 8 دقائق مسموح فعلًا في الجروبين وأنه لا يخالف Slow Mode أو قواعدهما.
