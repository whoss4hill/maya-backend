# Maya Backend - Setup Guide (aapke liye, step by step)

Ye file batati hai ki Maya ka backend khud kaise set up karna hai.
Developer hone ki zaroorat nahi hai - bas clicks hain, lagbhag 20-25 minute.
Kisi bhi step pe atko to Instinct se pooch lo.

Is folder me ye files hain:
- `schema.sql`     - database ke tables banane wala SQL (Supabase me run karna hai)
- `server.js`      - backend API ka code (Render pe chalega)
- `package.json`   - backend ko chalane ke liye zaroori list
- `render.yaml`    - Render ke liye ready-made settings
- `.env.example`   - kaunsi secret keys chahiye, unki list (asli keys kabhi file me NAHI dalni)
- `.gitignore`     - taaki secrets git me galti se na chale jayein

---

## Step 1: Supabase project banao (5 min)

1. https://supabase.com pe jao aur free account banao (Google se sign-in easiest hai).
2. "New project" dabao.
3. Naam: `maya` (kuch bhi rakh sakti ho).
4. Region: **Mumbai (ap-south-1)** choose karo - ye zaroori hai.
5. Database password khud set karo aur kahin safe likh lo (baar-baar nahi chahiye hoga, par rakh lo).
6. "Create new project" - 2 minute me ready ho jayega.

## Step 2: Database tables banao (2 min)

1. Supabase project ke left menu me **SQL Editor** kholo.
2. "New query" dabao.
3. Is folder ki `schema.sql` file ka POORA content copy karke paste karo.
4. **Run** dabao. "Success" aa jaye to tables ban gaye.

## Step 3: Login providers on karo (5-10 min)

1. Left menu me **Authentication > Providers** kholo.
2. **Email** already on hota hai - bas check kar lo.
3. **Google** on karo. Iske liye Google Cloud se OAuth client ID + secret
   chahiye hoga. Supabase dashboard step-by-step dikhata hai + ek redirect URL
   deta hai jo Google Cloud console me paste karni hai.
   (Ye sabse lamba step hai - atko to Instinct se saath me karwa lena.)

## Step 4: Supabase ki keys nikalo (1 min)

1. Left menu me **Project Settings > API** kholo.
2. Do cheezein copy karke kahin safe rakho (kisi ko mat bhejna):
   - `Project URL`  (https://....supabase.co jaisa)
   - `service_role` key (secret wali - sirf backend ke liye)
3. `anon` `public` key bhi note kar lo - ye baad me app (APK) me lagegi.

## Step 5: Gemini API key banao (2 min)

1. https://aistudio.google.com pe jao, apne Google account se sign in karo.
2. "Get API key" > "Create API key" dabao.
3. Key copy karke safe rakh lo. Ye sirf backend me lagegi.

## Step 6: Code ko GitHub pe daalo (5 min)

1. https://github.com pe apna account banao (agar nahi hai).
2. "New repository" > naam `maya-backend` > **Private** select karo > Create.
3. "uploading an existing file" link dabao.
4. Is folder ki saari files drag-drop karke upload kar do
   (`.env.example` aur `.gitignore` bhi; `.env` naam ki koi file kabhi nahi).
5. "Commit changes" dabao.

## Step 7: Render pe backend chalao (5 min)

1. https://render.com pe account banao (GitHub se sign-in easiest hai).
2. "New +" > "Web Service" > apna `maya-backend` repo connect karo.
3. Render `render.yaml` ko khud pehchan lega. Plan: **Free**.
4. "Environment" section me ye 3 keys add karo (Step 4 aur 5 se):
   - `SUPABASE_URL` = apna Project URL
   - `SUPABASE_SERVICE_ROLE_KEY` = apni service_role key
   - `GEMINI_API_KEY` = apni Gemini key
5. "Deploy" dabao. 2-3 minute me live ho jayega.
6. Upar jo URL milega (jaise https://maya-backend.onrender.com), uske aage
   `/health` lagake browser me kholo. `{"ok": true}` dikha to backend zinda hai.

## Ek baat dhyan rakhna

Render ka free plan 15 minute idle rehne pe so jata hai. Pehla request
jaagne me 20-30 second le sakta hai - ye normal hai, error nahi hai.
(Baad me chaaho to ek free uptime monitor laga sakte hain jo use jagata rahe.)

## Kabhi bhi ye rules

- Service-role key aur Gemini key SIRF Render ke Environment settings me.
  Kabhi chat me nahi, kabhi GitHub me nahi, kabhi app me nahi.
- Anon key sirf app (APK) me lagegi - wo public ke liye bani hai.
- Koi key leak ho jaye to wahi dashboard se turant rotate kar do.
