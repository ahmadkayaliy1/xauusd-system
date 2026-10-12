<!-- prompt: scalp/instructions · version 3 -->
Time now: $now_utc. Reason: $reason (hourly = the scheduled brief at the top of the hour; dashboard = the owner asked for one now; manual = a run by hand).

## The desk (the automatic trader on the same account)
$desk

## The owner's lot and the execution of your trade
$execution

## The owner's recent manual trades
$manual_recent

## Your journal (previous briefs, scored on the real ticks)
$journal

## Charts
$charts_note

## Payload (the same snapshot the desk reads; compact view)
$payload

## Answer

Fill every field of the schema. اكتب النصوص (`picture`، `expect`، `why`، `trigger`، `invalidation`، `no_scalp`، `news_clock`، `desk_warning`، `confidence_note`) بلغة $output_language، والمصطلحات التقنية بالإنكليزي، والأرقام أرقام لا كلمات. الصورة أولاً: شو عم يصير هالساعة وشو المتوقع، بعدها المستويات بأسبابها، بعدها الخطط المشروطة - وإذا ما في سكالب هالساعة قول `no_scalp_now: true` واترك `plans` فاضية. وبـ `trade_call` اكتب الصفقة الوحدة اللي بتاخدها هلق: من وين تفوت (السعر والطريقة)، وين الوقف، وين الهدف، شو الشرط قبل الدخول، وكم دقيقة بتستنّى - نفس أرقام الخطة اللي بيشير إلها `best_plan`؛ وخلّي `picture` و`expect` يحكوا عنها بالكلام كمان. Prices in $price_reference prices. Return ONE JSON object and nothing else.
