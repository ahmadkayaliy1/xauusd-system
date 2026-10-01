# خطة: الدهب يتداول فعلياً على الـ demo (D-052)، وبعدها backtest على مستوى الـ tick

## Context
الدهب هلق shadow (‏D-049): بيحلّل وبيسجّل أفكار، بس ما بيبعت ولا أمر للوسيط. بدك تشوف كيف بيشتغل على أرض الواقع:
- **أولاً:** يتداول الدهب لحالو على حساب Windsor الـ demo، بإدارة كاملة للصفقة، وبمخاطرة أعلى، وبـ calls أكتر.
- **بعدها:** backtest احترافي. النظام نفسو بيشتغل على فترة من الماضي كأنها اليوم، وما بيشوف إلا البيانات اللي كانت موجودة بكل لحظة. التحليل بيصير بـ Claude فعلياً، والتنفيذ والإدارة بيتقيّموا tick بـ tick.

**قراراتك (2026-10-01):**
- مخاطرة الصفقة: 15% كحد أقصى.
- الخسارة اليومية: 30% كحد أقصى.
- بلا account drawdown stop.
- تداول طول اليوم (Asia كمان)، مع وعي بالـ session.
- 60 call باليوم مبدئياً.
- BTC/ETH بيضلّوا موقّفين.
- الأموال الحقيقية بتضل قرارك لحالك (‏H9). هالخطة كلها **demo بس**.

## المشاكل اللي لازم نكون عارفينها (مرتّبة حسب الخطورة)
1. **الحد الأدنى للعقد:** ‏0.01 lot = 1 oz. على $107، الـ stop الهيكلي ($7–15) بيخاطر بـ 7–14% بالصفقة.
   - خسارتين بيسكّروا النهار (بسقف الـ 30%).
   - وبلا drawdown stop، الحساب فيه ينزل كتير بكم يوم سيئين. مقبول لأنو demo، بس لازم نقيسو.
2. **الـ leverage:** صفقة 1 oz قيمتها ≈ $4,200، يعني ~39× على $107، و~56× على $75. السقف الحالي 10×.
   - بنقترح سقف **60×**. هاد بيعمل أرضية طبيعية: تحت ~$70 equity الـ gate بيرفض الدهب.
3. **الـ monitor رح يوقّف النظام:**
   - بيشغّل الـ global KILL_SWITCH إذا الـ equity نزلت >10% بين دورتين (15 دقيقة). صفقة وحدة خسرانة بـ 11–15% بتعمل هيك.
   - والـ `correlated_exposure` (‏4%) بيرفض كل صفقة دهب.
   - لازم يرتفعوا الاتنين.
4. **‏Partial close مستحيل بـ 1 oz**، لأنو الـ volume_step هو 0.01.
   - الإدارة رح تكون: breakeven، وtrailing (ATR/structure)، وclose_all، وtime stops.
   - وكمان أفعال Claude على الصفقة المفتوحة: modify_sl للتضييق، وmodify_tp، وclose، وcancel.
   - الـ TPs المتعددة بتنضغط لـ leg وحدة (‏D-044 5).
5. **حساب الـ demo مشترك مع BTC/ETH.** إذا رجعوا اشتغلوا، خسارات الدهب بتأثّر على الـ drawdown stop عندهم (25%)، لأنهم بيقروا الـ equity من الوسيط. **لازم يضلّوا موقّفين.**
6. **‏MT5 AutoTrading:** لازم يكون ON. في proposal بمجلد ETH بيحكي عن retcode 10027 (AutoTrading مطفي). منتحقق بأمر demo تجريبي.
7. **الاشتراك المشترك:**
   - 60 call × ~26k input ≈ 1.6M token باليوم، تقريباً 8M بأسبوع تداول، وفوقهن الـ backtest.
   - في خطر نوصل لـ session limits وقت شغلك.
   - الـ backtest بيشتغل بالويكند (سوق الدهب مسكّر)، وبسقف calls لكل تشغيلة.
8. **عطلة الويكند:** فجوة الأحد ممكن تتجاوز الـ stop (خسارة >15%). بنقترح قاعدة "flat before weekend" (تحت بالبند A3).
9. **اللابتوب:**
   - الـ RAM الحرّة ~550 MB.
   - النظام اشتغل ~21% من الوقت قبل ما تتسجّل المهام.
   - الـ backtest الطويل بيحتاج وقت هادي (ويكند).
10. **قاعدة المشروع "no mock data":**
    - الـ backtest بيستعمل ticks حقيقية مسجّلة.
    - تقويم الأخبار التاريخي لازم يكون من مصدر حقيقي. أداة FF الحالية بتعطي هالأسبوع بس.

---

## المرحلة A — الدهب يتداول على الـ demo (تقريباً 1–1.5 يوم بناء + مراجعة)

**مكان البناء:** worktree على `C:\TradingSystems\XAUUSD_wt\gold-live`، branch ‏`feat/gold-live-demo`. هاد مجلد الـ worktrees الرسمي لهالمشروع.
- النظام الشغّال (shadow) ما بينلمس.
- التطبيق بيصير بـ H36: ‏stop_all ← merge --ff-only ← config ← start_all.

### A1 — الإعدادات (`config/config.yaml`، مع تعليق D-052 جنب كل رقم)
- `pairs.XAUUSD.desk`:
  - `mode: live`
  - `windows: []`، يعني طول اليوم.
  - `reopen_grace_min: 60` بيضل، لأنو أول ساعة بعد افتتاح الأحد رقيقة.
  - `friday_cutoff` بيصير 15:00 New York.
  - جديد: `flat_before_weekend: {time: "16:30", tz: America/New_York}`.
- `risk`:

  | المفتاح | القيمة |
  |---|---|
  | `max_risk_per_trade_pct` | 15 |
  | `max_daily_loss_pct` | 30 |
  | `max_effective_leverage` | 60 |
  | `max_correlated_risk_pct` | 30 (مجموعة الدهب لحالو) |
  | `account_drawdown_stop_pct` | 100 (= موقوف) |

  الباقي بيضل متل ما هو: `min_rr 1.5`، و`min_confidence 55`، و`sl_atr_min_mult 0.5`، و`max_spread_to_sl_ratio 0.2`، و`max_open_positions 3`. الـ worst-case اليومي بيحدّ التزامن لصفقتين.
- `monitor`:
  - `equity_drop_warn_pct` بيصير 15.
  - `equity_drop_kill_pct` بيصير 35.
  - `daily_loss_warn_margin_pct` بيصير 5، يعني تحذير عند −25%.
- `config/config.local.yaml` (قسم `ai:`):

  | المفتاح | القيمة |
  |---|---|
  | `daily_calls_per_pair` | 60 |
  | `min_minutes_between_calls` | 15 |
  | `review_floor_minutes` | 10 |
  | `event_calls_per_day` | 12 |

  الـ `quota_reserve` بيضل.
- `.env` ما بينلمس: `demo/auto` موجودين فيه أصلاً.

### A2 — الكود (التغيير بأقل حد ممكن، كل شي shadow بيضل متل ما هو)
- `core/settings.py`:
  - `DeskCfg.mode` بيصير `Literal["shadow", "live"]`.
  - `windows: []` معناها "مفتوح طول ما السوق فاتح".
  - حقل `flat_before_weekend` اختياري.
  - نص الـ docstring بيتحدّث.
- `analysis/desk_windows.py::desk_window_state`: إذا `windows` فاضية، بيرجع `(True, "all_day")` بعد فحوصات السوق والـ reopen والـ Friday والأخبار.
- `execution/desk.py::shadow_pairs` بيضل `mode == "shadow"` بس. الـ desk الـ live بيمشي على مسار التنفيذ العادي (gate كامل، ما في `DESK_WAIVED_CHECKS`).
- `analysis/engine.py::_suppressed`: بالـ desk الـ live، الـ holdings غير المعروفة **ما بتنحسب "ولا شي"** (متل الأزواج العادية)، فالـ reviews تبع صفقة مفتوحة ما بتنكتم.
  - الـ stale news بيضل يوقّف الدخول (fail-closed، لأنو في مصاري).
- `api/app.py`: الـ 409 للـ shadow بس (موجود هيك أصلاً، ورح نتأكد).

### A3 — تاجر دهب احترافي: إضافات صغيرة على الإدارة
- **‏Pre-news review:** صفقة مفتوحة، وفي حدث `fomc/high` بعد ≤ 15 دقيقة ← event call لـ Claude يقرّر: يضيّق، أو يسكّر، أو يترك.
  - المكان: `engine` مع `analysis/news.py`، وبيتحسب من `event_calls_per_day`.
- **‏Flat before weekend:**
  - الجمعة 16:30 New York، الـ executor بيسكّر الصفقات المفتوحة ويلغي الـ pending، عن طريق `PositionManager`، وبيسجّلها `close_reason: weekend`.
  - قبلها بـ 30 دقيقة بيصير review call.
  - في config switch لتطفيه.
- الـ reviews التلقائية لصفقة مفتوحة (`next_review`، والـ events على fill/close) موجودة. منتأكد إنها بتشتغل برا كل نافذة (`windows: []`).

### A4 — الـ Prompts
- `ai/prompts/desks/xau.md` بيصير v2 "live on demo":
  - الصفقات بتتنفّذ بـ 1 oz، بسقف 15%/صفقة و30%/يوم.
  - stop هيكلي حقيقي، وما في تصغير للـ stop كرمال يركب.
  - single leg بـ 1 oz، يعني ما في partials.
  - إرشادات session لـ 24 ساعة:
    - Asia: range، وسيولة أقل، وsweeps.
    - London open: sweep للـ Asia range.
    - NY open والأخبار.
    - rollover من 21:00 لـ 22:00 UTC: الـ spread بيتوسّع.
    - الجمعة والويكند.
  - الـ swap بالليل: الـ long بيكلّف.
- `desks/xauusd_fields.md`: الـ history صار صفقات منفّذة، والـ shadow fields بس للقديم.
- برومبت الـ operator/review بيقرا الـ desk mode (موجود)، ومنحدّث صياغتو.

### A5 — التستات
- **settings:** ‏`live` و`windows: []` و`flat_before_weekend`.
- **الـ gate بالأرقام الجديدة:**
  - stop 14% بيمرق.
  - 16% بينرفض.
  - leverage 61× بينرفض.
  - worst-case 30% شغّال.
- **‏executor:** الـ live desk بيعمل place على الـ paper backend. وحالة shadow ما تغيّرت.
- **‏engine:**
  - ما في كتم لـ reviews صفقة مفتوحة.
  - الـ pre-news event.
- **‏weekend close:** باستعمال `PositionManager`.
- **‏`tests/unit/test_split_pairs.py`:** قائمة صريحة بفروقات D-052 المسموحة لمجلد XAUUSD (risk، monitor، desk).
- الـ golden tests تبع BTC/ETH بيضلّوا متطابقين.
- الـ suite الكامل بـ below-normal priority.

### A6 — قبل التشغيل (خطواتك)
1. MT5 ← زر Algo Trading = ON.
2. `.venv\Scripts\python.exe tools\demo_order_test.py XAUUSD@`:
   - بيعمل أمر demo حقيقي 0.01، وبيلغيه، وبيفتح ويسكّر صفقة.
   - كلفتو ≈ الـ spread (~$0.2).
   - بيتأكد من الـ AutoTrading والـ magic والـ SL/TP.
3. ‏H36: ‏`scripts\stop_all.bat` ← `git merge --ff-only feat/gold-live-demo` ← `.venv\Scripts\python.exe -m tradingsystem config` ← `scripts\start_all.bat`.
4. **الرجوع:** `desk.mode: shadow` + `restart_all.bat`.
5. وطوارئ: `scripts\kill_switch_on.bat`.

### A7 — الـ Tracker
- ‏**D-052** (قرارك) بيحلّ محل D-049 لـ XAU على الـ demo، ومحل أرقام D-036/D-046(d) لـ XAU.
- ‏H32 بيصير ⛔ بالنسبة للـ demo.
- ‏H9 (أموال حقيقية) ما بيتغيّر.
- ‏H36 للتطبيق.
- ملاحظة: BTC/ETH لازم يضلّوا موقّفين.

---

## المرحلة B — Backtest على مستوى الـ tick (تقريباً 4–6 أيام بناء، بعد ما تشتغل A)

**الفكرة:** نفس كود الإنتاج بيشتغل على ساعة مُحاكاة (sim clock)، فوق data root منفصل بتنضخ فيه البيانات التاريخية "متل ما كانت عم توصل".
- النظام ما بيقدر يشوف ولا tick بعد اللحظة الحالية.
- Claude بيتسأل فعلياً.
- التنفيذ والإدارة بيمشوا tick بـ tick على الـ bid/ask الحقيقي.

**البيانات الموجودة:**
- ticks ‏`XAUUSD@`: من 2024-04-19 لهلق، 764 يوم، 2.2 GB، تقريباً 110–190k tick باليوم.
- كل الـ candles.
- `EURUSD@`/`XAGUSD@`: آخر 365 يوم بس، وقبلهن الـ payload بيقول "cross unavailable".

### المكوّنات (منعيد استعمال الموجود)
- **B1 — Sim clock:** ‏`core/timeutil.now_ms()` بيقرا override بيحطّو الـ runner بس.
  - كل الموديولات بتستدعي `now_ms` المستورد، فما في تغيير بأماكن الاستدعاء.
- **B2 — Market replayer:** ‏`tools/backtest/replay_market.py` بيعبّي `data/backtests/<run>/hot`:
  - الـ ticks بالترتيب.
  - كل شمعة بتنكتب بعد ما تسكّر، مع التأخير المقاس.
  - الـ cross candles.
  - التاريخ اللي قبل البداية: cold archive للقراءة بس (junction).
  - مصدر الكتابة: الـ writers الموجودين (`storage/sqlite_store`, `parquet_store`).
- **B3 — Runner:** ‏`tools/backtest/run.py` بعملية وحدة. لكل إغلاق 5m:
  - `Engine.tick()`، وفيه `orchestrator.run_cycle(..., as_of=now)`، وهي موجودة.
  - إذا انعمل call، الساعة بتمشي بقدّ الـ latency الحقيقية تبع Claude، وبتنضخ ticks هالفترة، وبعدين `Executor.step()`. يعني الدخول بالسعر اللي كان بعد ما وصل الجواب، متل الواقع.
  - بين الـ screens: `PaperBackend.process()` (موجود، tick بـ tick).
  - و`PositionManager` و`position_actions`.
  - والـ events على fill/close بتصحّي Claude متل الحي.
- **B4 — الحساب:**
  - ‏paper account برصيد بداية بتختارو (افتراضي $107.38) وبأرقام D-052.
  - منضيف الـ swap بالـ rollover (+ الأربعاء ×3) للواقعية.
- **B5 — الشغلات اللي ما إلها تاريخ:**
  - **تقويم الأخبار:** لازم مصدر حقيقي. مواعيد FOMC من الـ Fed، وNFP/CPI من جدول BLS، بملف `data/backtests/calendar_<period>.json` مع مصدرو.
  - إذا ما في مصدر: بيشتغل بلا blackout، وبينكتب هيك بالتقرير.
  - **‏operator notes / adaptive:** بيبلّشوا فاضيين.
- **B6 — الكلفة والحماية:**
  - ledger خاص بكل تشغيلة.
  - `--max-calls` (افتراضي 150).
  - تشغيل بالويكند.
  - **‏answer cache:** كل جواب بيتخزّن بوقتو المُحاكى. بعدها `--reuse-answers <run>` بيعيد التنفيذ والإدارة بإعدادات مختلفة **بلا ولا call**.
- **B7 — خطر التسريب (leakage):** إذا الفترة جوّا بيانات تدريب Claude، ممكن "يتذكّر" شو صار.
  - التخفيف: منبلّش بأحدث الشهور.
  - وخيار `--mask-dates` بيزيح السنة بالتواريخ اللي بيشوفها الموديل، والـ weekday والـ session بيضلّوا متل ما هني.
  - هاد تحذير بيضل مكتوب بكل تقرير.
- **B8 — التقرير** (`data/backtests/<run>/report.md` + json):
  - كل صفقة: الدخول والخروج بالـ tick، والـ R، والـ $، والإدارة اللي انطبقت.
  - منحنى الـ equity، والـ max DD، وP&L يومي.
  - حسب الـ session.
  - الـ calls والـ tokens.
  - المقارنة مع الـ random baseline: منعيد استعمال `tools/desk_report.py`.
- **B9 — المعايرة (calibration):** منعيد تشغيل أيام عنّا فيها قرارات حيّة (مثلاً 2026-10-01)، ومنقارن الـ triggers والقرارات والتنفيذ مع اللي صار فعلياً.

### الوقت والكلفة المتوقعة
- يوم تداول ≈ 30–60 call × ~80 s، يعني 40–80 دقيقة، و0.8–1.6M token.
- أسبوع ≈ 4–7 ساعات و4–8M token.
- منبلّش بيوم واحد، وبعدين أسبوع.

### تستات B
- **‏Causality guard:** وقت الـ backtest، أي قراءة بعد `now` بترمي error.
- **‏Determinism:** إعادة التشغيل بالـ answer cache بتعطي نفس الصفقات بالضبط.
- يوم صغير end-to-end بلا Claude: الـ answers من fixture حقيقي مخزّن.

---

## الترتيب
1. ‏A1–A5 بالـ worktree، ومراجعة بعدسة وحدة (money path)، والـ suite.
2. أنت: A6 (Algo Trading، والأمر التجريبي، وH36).
3. أسبوع–أسبوعين demo: منقيس الـ fills والـ slippage والإدارة والـ R والـ calls، وبتقرر إذا منزيد الـ calls أو منعدّل.
4. ‏B1–B4 + B6 + B8، بعدين B5/B7/B9. أول تشغيلة: يوم واحد بالويكند.

## التحقق
- **‏A:**
  - ‏`pytest tests/unit` كامل.
  - `tradingsystem config` بيبيّن `desk mode=live` والأرقام.
  - ‏`demo_order_test.py XAUUSD@` ناجح.
  - بأول ساعة بعد H36: ‏`health_report --hours 1` نظيف، وأول صفقة بالـ magic 26092504 مع SL/TP، والـ monitor بلا KILL_SWITCH.
- **‏B:**
  - ‏causality guard + determinism بالتستات.
  - تشغيلة يوم 2026-10-01 مقابل اللي صار فعلياً.
  - التقرير بيطلع مع كلفة الـ tokens.
