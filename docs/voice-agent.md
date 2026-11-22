# AI voice agent (Tunisian Darija phone call)

When a customer has not answered the WhatsApp confirmation and the order is *Sans réponse*,
COD Confirm can call them **once**, between 8:00 and 22:00, never after a STOP. An ElevenLabs
voice agent asks them to confirm or cancel, and the result updates the order (*Confirmée* /
*Annulée*, history note "appel vocal"). If the answer is unclear, the order stays *Sans réponse*
and the owner calls the customer.

The operator sets it up once for the whole platform. Each shop then switches it on in
**Paramètres → Appel vocal**.

## 1. Phone number

ElevenLabs places the call through a number you import:

- **Twilio**: buy a number that can call Tunisia (+216). Then in ElevenLabs go to
  *Phone numbers* → *Import* → *Twilio*.
- **SIP trunk** (a Tunisian VoIP operator, usually cheaper per minute): *Import* → *SIP trunk*,
  and set `ELEVENLABS_TELEPHONY=sip-trunk`.

Copy the **phone number id** that ElevenLabs shows.

## 2. Agent

Create an agent in **ElevenLabs → Agents**:

- **Language**: Arabic (`ar`). Pick a voice that sounds Maghrebi or Tunisian (*Voice library*,
  filter Arabic), or clone a Tunisian voice, with that person's consent.
- **First message**:

  ```
  عسلامة {{customer_name}}، معاك المساعد الآلي متاع {{shop_name}}. نكلمك على الكوموند اللي عملتها: {{products}}، بـ {{total}} دينار، توصلك لـ {{city}}. تأكدهالي؟
  ```

- **System prompt**:

  ```
  إنتي مساعد آلي متاع محل {{shop_name}} في تونس. تحكي كان بالدارجة التونسية، بجمل قصيرة وبلطف،
  كيما يحكي تونسي في التليفون. ما تحكيش بالفصحى.

  الهدف الوحيد: الحريف يأكد ولا يلغي الكوموند متاعو.
  - الكوموند: {{products}}، المجموع {{total}} دينار، الخلاص كي توصل (الدفع عند الاستلام).
  - العنوان: {{address}}، {{city}}.

  القواعد:
  - إذا قال "إيه / باهي / أكيد / نأكد" → قلو "يعطيك الصحة، الكوموند تأكدت وتوصلك قريب" وسكّر.
  - إذا قال "لا / نلغي / ما عادش نحب" → قلو "باهي، ألغيناها. نهارك زين" وسكّر.
  - إذا حب يبدل حاجة (عنوان، قطعة، كمية) → قلو "باهي، المحل باش يكلمك يبدّلها" وسكّر.
  - إذا ما فهمتش، عاود السؤال مرة برك. إذا مازال مش واضح، قلو "المحل باش يكلمك" وسكّر.
  - إذا الحريف قال إنو ما عملش كوموند → قلو "سامحنا على الإزعاج" وسكّر.
  - ما تعطيش معلومات أخرى، ما تبيعش، وما تطلبش أرقام بطاقات ولا معطيات بنكية أبداً.
  - المكالمة ما تفوتش دقيقتين.
  ```

- **Analysis → Data collection**: add one item.
  - Identifier: `decision`
  - Type: string
  - Description: `"confirmed" if the customer clearly confirmed the order, "cancelled" if they clearly cancelled it or said they never ordered, otherwise "unclear" (wants a change, did not understand, voicemail, hung up).`
- **Max conversation duration**: 120 s.

Copy the **agent id**.

## 3. Post-call webhook

In ElevenLabs → *Settings* → *Webhooks*, add the URL `https://<your-domain>/webhooks/voice`.
Select the events **transcript** and **call_initiation_failure**. Do not select audio: it is
large and the app does not use it. Turn retries on, and copy the **secret**.

## 4. Environment

```
ELEVENLABS_API_KEY=...
ELEVENLABS_AGENT_ID=...
ELEVENLABS_PHONE_NUMBER_ID=...
ELEVENLABS_TELEPHONY=twilio        # or sip-trunk
ELEVENLABS_WEBHOOK_SECRET=...
```

Restart the app. The *Appel vocal* switch then appears in every shop's settings.

## Before selling it

- **Test the Darija first.** ElevenLabs lists Arabic, but it does not document how well it
  understands Tunisian dialect. Call your own phone about twenty times with different accents
  and answers ("ey", "behi", "la", "nbadel l'adresse", French mixed in). Adjust the prompt and
  the voice until it holds up.
- **Cost**: you pay ElevenLabs (agent minutes) plus the call itself (Twilio or SIP). Calls to
  Tunisian mobiles are not cheap: price it as an add-on or reserve it for the higher plans.
- The agent says up front that it is an automated assistant. Keep that line: customers need to
  know.
