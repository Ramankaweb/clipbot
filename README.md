# ClipBot – 24/7 live stream clipper (Render + Telegram)

Live link do → bot stream record karta rehta hai (rolling buffer) → trigger hote hi **pichle 15 sec** ki clip Telegram par aati hai → file delete.

## Triggers
| Trigger | Kaise |
|---|---|
| **Chat command** | YouTube live chat me likho: `botname clip cut (title)` – title clip ke caption me aata hai |
| **Audio keyword** | Har ~15 sec ka audio Groq Whisper se transcribe; "kill", "paani", "headshot" etc. aaye to auto clip |
| **Manual** | Telegram `/clip title` ya web panel ka "Clip now" button |

Subscriber-only chat ON ho to bhi chat *padhi* ja sakti hai (bot apna chat reader use karta hai, login nahi chahiye). Sirf subscribers likh sakte hain, jo ek natural filter ban jata hai. Panel me "sirf mod/owner/member" ka option bhi hai. Members-only *stream* me chat bina login ke nahi dikhti.

## Deploy (Render)
1. Ye folder GitHub repo me push karo.
2. Render → **New → Blueprint** → repo select. `render.yaml` apne aap service + disk bana dega.
3. Env var me **ADMIN_PASSWORD** daalo (lamba rakho, min 10 char).
4. Deploy hone ke baad `https://<name>.onrender.com` kholo → password se login.
5. **Settings** me Telegram bot token (@BotFather se) + Groq key (console.groq.com, free) daalo → Save.
6. Telegram me apne bot ko bhejo: `/start <ADMIN_PASSWORD>` → tum owner register ho jaoge. Uske baad bot **sirf tumhara** hai, baaki sab ignore.
7. Live link web panel me daalo ya seedha Telegram me bhej do.

### 24/7 kaise chalega
- `/health` endpoint Render health check ke liye hai.
- **Starter plan (~$7/mo) lo.** Free plan 15 min inactivity par sleep hota hai aur disk bhi nahi milti, to bot 24/7 nahi chal sakta. Free par bhi chalana ho to UptimeRobot se `/health` har 5 min ping karwao, par wo reliable nahi hai.
- Restart/redeploy ke baad active links apne aap resume hote hain (disk par save hote hain).

## Telegram na chale to
1. Web panel Dashboard ke **Telegram** card me status dekho (connected / token galat / conflict).
2. Bot ko `/start <ADMIN_PASSWORD>` bhejo (sirf Start button dabana kaafi nahi, password dena hoga).
3. "Conflict" dikhe to isi token par koi aur bot (purana t1 bot, Termux, doosra Render instance) chal raha hai, use band karo ya @BotFather se naya token lo.

## Extra features
- `/menu` – buttons (Status, Clip now, Stop all, Audio on/off, Ping)
- **Vertical reel output** – Settings > Output format: `vertical_blur` (blur background) ya `vertical_crop`, 1080x1920
- **Post-roll** – trigger ke baad 0-30 sec ka action bhi clip me
- **Offline stream ka intezaar** – channel `/live` link do, live aate hi bot khud shuru (Settings > retry)
- **Allowed users** – sirf chune hue YouTube naam clip command de sakein

## Telegram commands
- Link bhejo ya `/watch <link>` – watch shuru
- `/stop [id|all]`, `/status`
- `/clip [title]` – abhi ki last clip
- `/audio on|off`
- `/set <key> <value>` – bot_name, clip_seconds, cooldown_seconds, keywords, audio_enabled, chat_enabled, chat_who
- API keys **sirf web panel** se badalti hain (Telegram me secret bhejna safe nahi).

## Web panel features
Dashboard (link add, stop, clip now, recent clips, live log) • Settings (saari API keys, cookies, bot name, clip length, keywords, limits) • Telegram/Groq test buttons • yt-dlp update button • admin password change.

## Privacy / security
- Panel password se locked; 5 galat koshish par 5 min lock; CSRF protection; secure cookies.
- API keys panel me kabhi wapas dikhayi nahi jaati (sirf "✓ saved"); logs me bhi mask hoti hain.
- Settings file disk par `chmod 600`.
- Telegram bot sirf registered owner ko jawab deta hai.

## Dhyan dene wali baatein
- **YouTube Render ke datacenter IP ko block karta hai.** "Sign in to confirm you're not a bot" aaye to Settings me YouTube `cookies.txt` paste karo (**alag/throwaway Google account** se export karo, apna main nahi). Phir bhi kabhi-kabhi dikkat aa sakti hai; residential proxy hi pakka hal hai (`yt-dlp extra args` me `--proxy ...`).
- Telegram video limit **50 MB**: 15 sec 720p clip aaram se chhoti rahti hai.
- Groq free tier ki limit hai; panel me "roz ke max requests" set karo.
- Stream URL kuch ghante baad expire hota hai; bot khud re-resolve karke restart karta hai (kuch sec ka gap aa sakta hai).
- Sirf wahi streams record karo jinka tumhe adhikaar ho ya jinki permission ho; clips ko reupload karne se pehle creator ke rules dekho.
- Local test: `pip install -r requirements.txt && ADMIN_PASSWORD=yourpass python app.py` (ffmpeg + yt-dlp installed hone chahiye).
