# نقل نظام الدهب XAUUSD للابتوب جديد (دليل الانتقال)

**القاعدة الأولى:** النظام ما بيشتغل على جهازين بنفس الوقت أبداً. نفس الحساب، نفس رقم الـ magic، نفس الـ terminal:
نظامين بيتخانقوا على نفس الصفقات. القديم بيوقف **قبل** ما الجديد يشتغل.

الدليل مكتوب لطريقتين: **(أ) نسخ المجلد كلّو متل ما هو** (الأسهل)، أو **(ب) سحب الكود من GitHub واسترجاع الحالة** من النسخة
الاحتياطية. بالحالتين في أشياء لازم تنعاد على الجهاز الجديد (الـ venv، المهام المجدولة، MT5، Claude).

---

## المرحلة 1 - على اللابتوب القديم

1. تأكّد ما في صفقة مفتوحة ولا أمر معلّق (تبويب الحي بالـ dashboard، أو تطبيق MT5).
2. وقّف كل شي:
   ```
   scripts\stop_all.bat
   scripts\stop_scalp.bat
   ```
3. نسخة احتياطية طازجة للحالة (قواعد البيانات، سجل الذكاء، `config.local.yaml`، المراجعات، حالة المراقب):
   ```
   .venv\Scripts\python.exe tools\backup_state.py
   ```
   بتطلع بـ `backups\<وقت UTC>.zip`. ملاحظة: الـ zip **ما فيه** `.env` ولا `data\scalp` ولا بيانات السوق.
4. ألغِ المهام المجدولة حتى القديم ما يرجع يشتغل لحالو بعد إعادة تشغيل:
   ```
   scripts\uninstall_autostart.bat
   ```
   وبعدها من Task Scheduler عطّل أو احذف كل مهام المجلد `\TradingSystems\XAUUSD\` (`TradingSystemOps-Monitor`،
   `TradingSystemOps-Backup`، `TradingSystemOps-Recorder`، `TradingSystemOps-ReviewDaily/Weekly`).
5. انسخ للجهاز الجديد (فلاشة أو درايف خارجي):
   - **الطريقة (أ):** مجلد `C:\TradingSystems\XAUUSD` كلّو. حجمو الأساسي ≈ 4.5 GB، أغلبو `data\cold` (3.4 GB، بيانات تاريخية
     للاختبارات، اختيارية) و`data\hot` (≈ 0.5 GB، الشموع والتكات، انسخها). بتقدر تستثني `logs\` و`.venv\` (بتنعاد).
   - **الطريقة (ب):** `backups\` (آخر zip على الأقل) + **`.env`** + `data\scalp\` + `data\hot\` (+ اختياري `data\cold\`،
     `data\backtests\`).
   - **`.env` هو الأسرار** (توكن Telegram، `EXECUTION_MODE`، `DASHBOARD_TOKEN`، بيانات الحسابات إذا موجودة). مش على GitHub
     ومش بالـ zip. بالطريقة (أ) هو جوّا المجلد المنسوخ: احمِ النسخة (فلاشة ما تضيّعها، واحذفها منها بعد النقل).

---

## المرحلة 2 - تجهيز اللابتوب الجديد

### 2.1 البرامج
| البرنامج | ليش | ملاحظة |
|---|---|---|
| **Python 3.12** (بالضبط 3.12، مش 3.13) | المشروع (`requires-python >=3.12,<3.13`) | فعّل "Add to PATH" |
| **Git** | الكود من GitHub (الطريقة ب) والتحديثات لاحقاً | |
| **MetaTrader 5** | التنفيذ والأسعار | بنفس المسار `C:\Program Files\MetaTrader 5\terminal64.exe` (وإلا عدّل `mt5.profiles.*.terminal_path`)، سجّل دخول الحساب المطلوب، فعّل **Algo Trading**، خلّي "احفظ كلمة السر" حتى يفتح لحالو بعد إعادة التشغيل |
| **Claude Code CLI** | النظام الحي وScalp بيسألوا الموديل من خلالو (اشتراك Max) | ثبّتو وسجّل الدخول بحسابك: `claude` ثم `/login`؛ جرّب `claude -p "ok"` |
| **ExpressVPN** (إذا بتستعملو) | المراقب بيراقب كرت الشبكة تبعو | اسم الكرت بيختلف بالجهاز الجديد (شوف 2.4) |
| Windows PowerShell 5.1 | تنبيهات الـ toast | موجود بويندوز أصلاً |

### 2.2 الكود والـ venv
- **الطريقة (أ):** حطّ المجلد المنسوخ بـ `C:\TradingSystems\XAUUSD` (نفس المسار المهم: المهام والسكريبتات بتستعملو)،
  **واحذف `.venv` القديم** (مش قابل للنقل بين جهازين) وأعد بناءه:
  ```
  cd C:\TradingSystems\XAUUSD
  rmdir /s /q .venv
  py -3.12 -m venv .venv
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -e .
  ```
- **الطريقة (ب):**
  ```
  git clone https://github.com/ahmadkayaliy1/xauusd-system.git C:\TradingSystems\XAUUSD
  cd C:\TradingSystems\XAUUSD
  py -3.12 -m venv .venv
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -e .
  ```
  ثم حطّ `.env` بالمجلد، واسترجع الحالة من الـ zip:
  ```
  .venv\Scripts\python.exe tools\backup_state.py --restore <مسار>\<وقت>.zip --target data --config-dir config
  ```
  وانسخ `data\scalp\` و`data\hot\` (والاختيارية) لمكانها.

### 2.3 فحص الإعدادات
```
.venv\Scripts\python.exe -m tradingsystem config
```
لازم يطبع الإعدادات بلا خطأ. إذا طلع خطأ عن `execution.mode=live`: السطر `EXECUTION_MODE` بـ `.env` و`execution.mode` بـ
`config\config.local.yaml` لازم يتطابقوا (`live` مع عبارة التأكيد، أو `demo`).

### 2.4 شو بيتغيّر بالجهاز الجديد (بـ `config\config.local.yaml`)
- `monitor.vpn_adapter_names`: اسم كرت الـ VPN بالجهاز الجديد (اعرفو بـ
  `.venv\Scripts\python.exe -c "import psutil;print(list(psutil.net_if_addrs()))"`)، أو خلّيها `[]`.
- `monitor.free_ram_warn_mb` و`scalp.min_free_ram_mb`: حسب ذاكرة الجهاز الجديد (كانوا 600 و350 للابتوب 8 GB).
- `mt5.profiles.*.terminal_path` إذا MT5 بمسار تاني.
- الحساب: الـ terminal لازم يكون على الحساب اللي بيطابق الوضع (`live` = الحقيقي، `demo` = التجريبي)، وإلا النظام بيرفض يشتغل
  (هاي حماية مقصودة). التبديل بين الحسابين: `scripts\switch_mode.bat demo|live` (الدليل §14).

### 2.5 إعدادات ويندوز (الأهم لاستمرار الشغل)
```
scripts\check_ops.bat
```
بيفحص: النوم عند إغلاق الغطا وزر الطاقة (على الكهربا **وعلى البطارية**)، النوم بعد الخمول = أبداً، توفير طاقة الواي فاي =
أداء أقصى، مزامنة الساعة، ساعات Windows Update. التفاصيل بـ `docs\ops_windows.md` §0–§6. الشاحن دايماً موصول.

### 2.6 المهام المجدولة
```
scripts\install_autostart.bat          (تشغيل النظام تلقائياً عند الدخول وكل 5 دقايق إذا وقع + فتح MT5 عند الدخول)
scripts\install_operator_tasks.bat     (المراقب كل 15 دقيقة، النسخ الاحتياطي اليومي 03:30 UTC، مهام المراجعة)
```
بعدها عطّل مهمّتي `TradingSystemOps-ReviewDaily` و`-ReviewWeekly` (بيصرفوا من الاشتراك بلا لزوم):
```
schtasks /Change /TN "\TradingSystems\XAUUSD\TradingSystemOps-ReviewDaily" /Disable
schtasks /Change /TN "\TradingSystems\XAUUSD\TradingSystemOps-ReviewWeekly" /Disable
```
حلقة Scalp ما إلها مهمة مجدولة: بتشغّلها بإيدك `scripts\start_scalp.bat` (وبعد كل إعادة تشغيل).

---

## المرحلة 3 - التشغيل والتحقّق

1. `scripts\start_all.bat` ثم افتح `http://127.0.0.1:8768`: تبويب الحي لازم يبيّن supervisor / engine / executor / mt5 كلّن
   `live`، والـ executor على الوضع الصح (demo/live) ونفس رقم الحساب.
2. تنبيه تجريبي: `.venv\Scripts\python.exe tools\notify.py --level info --title Test --text Hello` → لازم يطبع
   `telegram: sent` (وإلا راجع `.env` وقسم 6 بـ `docs\notifications.md`).
3. إذا بدّك Scalp: `scripts\start_scalp.bat` ثم تبويب Scalp (إعداداتك محفوظة بـ `data\scalp\settings.json`).
4. تقرير الصحة: `.venv\Scripts\python.exe tools\health_report.py --hours 1`.
5. بعد ما تتأكّد إنو الجديد شغّال: **ما ترجّع تشغّل القديم أبداً**. إذا بدّك تحتفظ فيه كأرشيف، خلّي `.venv` محذوف فيه
   والمهام ملغاة.

---

## شو بيضيع إذا ما نقلتو
| الملف/المجلد | إذا ما انتقل |
|---|---|
| `.env` | ولا Telegram، ولا الوضع الصح، ولا توكن الـ dashboard: النظام بيشتغل على الافتراضيات (demo/paper) |
| `config\config.local.yaml` | بترجع الإعدادات للافتراضية (الوضع، ساعات Scalp، حدود الذاكرة، السيرفر الحقيقي) |
| `data\instances\XAUUSD\app.db` | ذاكرة النموذج: القرارات، الصفقات، الدروس، المراجعات. (بالـ zip) |
| `data\shared\ai_usage.db` | سجل الأسئلة وحساب الكلفة. (بالـ zip) |
| `data\scalp\` | إعدادات Scalp، الـ briefs، نسبة الإصابة، سجل التنفيذ التلقائي. (مش بالـ zip: انسخه) |
| `data\hot\` | الشموع والتكات: النظام بيعيد تحميل ما بيقدر من الوسيط، بس التاريخ الطويل (الشموع اليومية للسنة) ممكن ما يرجع كامل |
| `data\cold\`, `data\backtests\` | بيانات واختبارات قديمة: ما بتأثّر عالتداول |
