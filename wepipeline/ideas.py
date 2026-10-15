"""Call ideas for GMs: data point -> angle, in Hinglish. Not a script: the GM uses their own words.

Each idea is {"fact": what the data says, "angle": how to use it on the call}. Ideas are ranked, strongest first.
Compliance: facts only, no promises ("returns guaranteed nahi"), never delay or discourage a payout.
"""
from __future__ import annotations


def _when_hi(days: float) -> str:
    if days < 1 / 24:
        return "abhi abhi"
    if days < 1:
        return f"{int(days * 24)} ghante pehle"
    return "kal" if days < 2 else f"{int(days)} din pehle"


def _gap_hi(days: int) -> str:
    if days < 60:
        return f"{days} din"
    if days < 365:
        return f"{days // 30} mahine"
    years = days // 365
    return f"{years} saal se zyada"


def build(d: dict) -> list[dict]:
    """d holds the facts computed in insights.customer_insight (see the keys used below)."""
    s, seg, stage = d["sname"], d["seg"], d["stage"]
    seg_txt = seg or "equity, F&O, commodity"
    out: list[tuple[int, str, str]] = []  # (rank, fact, angle): lower rank = more important

    def add(rank, fact, angle):
        out.append((rank, fact, angle))

    # --- Sarthi activation: Sarthi is off; show it and switch it on together. No funding push on this call
    if stage == "sarthi_activation":
        a = d.get("activation") or {}
        if d.get("sarthi_interest"):
            add(0, f"{s} off, phir bhi interest: {d['sarthi_interest'].replace(' in 30 days, with Sarthi off', '')}",
                f"Batao: {s} on hoga toh yahi ideas seedha app pe aayenge, entry-target-stop-loss ke saath. Abhi on kar dete hain?")
        if a.get("total_trades"):
            add(1, f"{a['total_trades']} trades, {a.get('active_days', 0)} din active (last {a.get('window_days', 60)} din)",
                f"Aap already active trader ho; {s} ke ready ideas research ka time bachayenge, faisla aapka hi rahega")
        if d.get("campaign_engaged"):
            add(1, f"{s} campaign pe response: {d['campaign_engaged']}", "Usi message se shuru karo: \"Aapne hamara Sarthi message dekha tha\"")
        if d.get("record_hi"):
            add(2, d["record_hi"], "Number jaisa hai waisa batao; past results future ki guarantee nahi")
        if seg:
            add(3, f"Pasand ka segment: {seg}", f"{seg} ke {s} ideas dikhao")
        add(4, f"{s} on karna", "App mein 1 minute: call pe hi saath mein on karwao. On karne se koi trade place nahi hota")
        out.sort(key=lambda x: x[0])
        return [{"fact": f, "angle": a2} for _, f, a2 in out][:6]

    # --- referral ask: an active trader; thank, then ask. No funding push on this call
    if stage == "referral":
        r = d.get("referral") or {}
        if d.get("campaign_engaged"):
            add(0, f"Referral campaign pe response: {d['campaign_engaged']}",
                "Usi message se shuru karo: \"Aapne hamara refer-a-friend message dekha tha, socha khud baat kar loon\"")
        coh, tg, h = d.get("referral_cohort") or "", set(d.get("referral_tags") or []), d.get("referral_history") or {}
        if coh == "C1c":
            add(0, f"Haal hi mein {h.get('referred', 0)} dost refer kiya, abhi trade shuru nahi hua",
                "Shukriya bolo; poochho dost ko account / funding mein madad chahiye? Phir: \"Koi aur dost?\"")
        if coh in {"C1a", "C1b"}:
            add(0, f"Pehle {h.get('referred', 0)} dost refer kiye, {h.get('active', 0)} ne trade kiya",
                f"Shukriya se shuru karo: \"Aapke refer kiye {h.get('active', 0)} dost ne hamare saath trading shuru ki, thank you\"")
        if coh == "C1b" or ("dormant" in tg and coh not in {"C2"}):
            pr = d.get("referral_profile") or {}
            gap = ""
            if pr.get("ltd"):
                from datetime import datetime as _dt
                try:
                    days = (_dt.now().date() - _dt.fromisoformat(pr["ltd"][:10]).date()).days
                    gap = f" ({days // 30} mahine se)" if days >= 60 else f" ({days} din se)"
                except ValueError:
                    gap = ""
            add(0, f"Trade nahi kiya{gap}",
                "Pehle reconnect: poochho kya badla, kya wapas laa sakta hai (charges, loss, time, app). Referral tabhi poochho jab "
                "baat achhi chale")
            if seg:
                add(1, f"Pasand ka segment: {seg}", f"{seg} ke {s} ideas dikhao: \"Wapas shuru karna ho toh ready ideas milenge\"")
        elif coh == "C2":
            add(0, "Naya client (1-6 mahine), trade kar rahe hain",
                "Onboarding check-in: experience kaisa raha, koi dikkat? Madad ke baad hi referral poochho")
        elif r.get("total_trades"):
            add(0, f"{r.get('total_trades', 0)} trades, {r.get('active_days', 0)} din active (last {r.get('window_days', 60)} din)",
                "Pehle shukriya bolo: aap hamare sabse active clients mein ho. Poochho experience kaisa hai, koi problem toh nahi")
        elif coh not in {"C1a"}:
            add(0, "Swastika ke saath hain", "Pehle shukriya bolo; poochho experience kaisa hai, koi problem toh nahi")
        if "sarthi" in tg:
            add(1, f"{s} use karte hain", f"Angle: \"Aapke dost ko bhi wahi {s} ideas milenge jo aap dekhte ho\"")
        if "heavy" in tg and coh != "C4":
            add(2, "Active trader", "Unke trading circle ke dost sabse sahi: jo already trade karte hain ya seekhna chahte hain")
        add(1, "Referral ka sahi moment",
            "Seedha poochho: koi dost / family jo trading karte hain ya shuru karna chahte hain? 1-2 naam aur number, "
            "unki permission se")
        add(2, "Dost ko kya milega", "Discount brokerage, " + s + " ke ready trade ideas, aur account kholne mein poori madad"
            + (f". {d['pricing_hi']}" if d.get("pricing_hi") else ""))
        if d.get("reward_hi"):
            add(1, "Dono ko reward", f"Batao: {d['reward_hi']}. Isse zyada kuch promise mat karo")
        if d.get("sarthi_active") is False:
            add(4, f"{s} on nahi hai", f"Baat ke end mein {s} on karwa do, ideas seedha app pe aayenge")
        add(5, "Feedback mile toh", "Note karo aur SM ko batao; problem pehle, referral baad mein")
        out.sort(key=lambda x: x[0])
        return [{"fact": f, "angle": a} for _, f, a in out][:6]

    # --- payout: retention first, and the payout is never blocked
    if stage == "payout":
        amt = f"₹{d['payout_amount']:,.0f} ka " if d.get("payout_amount") else ""
        when = f" ({d['payout_date']})" if d.get("payout_date") else ""
        add(0, f"{amt}payout request kiya{when}",
            "Pehle bolo payout normal process hoga, koi rukawat nahi. Phir seedha puchho: paise kyun nikal rahe hain? Wajah samjho")
        add(1, "Wajah fix ho sakti hai toh",
            "Problem solve karo (charges, loss, app, kya trade karun), phir poochho: payout request cancel ya kam karna chahenge? "
            "Sirf unki marzi se")
        if d.get("pricing_hi"):
            add(3, "Wajah charges ho sakti hai", f"Agar charges ki baat aaye: {d['pricing_hi']}")
        add(4, f"Loss ya 'kya trade karun' wali problem ho sakti hai",
            f"{s} ke ideas stop-loss ke saath aate hain; chhote amount se try kar sakte hain (returns guaranteed nahi)")
        if d.get("rm_hi"):
            add(5, "Trades manage karna mushkil lag raha ho", f"Offer karo: {d['rm_hi']}")
        add(6, "Customer ki marzi sabse upar", f"Puchho, agar chaahein toh kuch balance {s} ke next idea ke liye account mein rakh sakte hain")

    # --- what they did in the app
    if d["orders_unfunded"]:
        add(1, f"{d['orders_unfunded']} order try kiye bina funds ke ({_when_hi(d['orders_days'])})",
            "Puchho order kyun nahi gaya. Funds add karwa do, UPI se 2 minute ka kaam hai")
    elif d["orders_funded"]:
        add(3, f"Recently {d['orders_funded']} order place kiye", "Check karo order execute hue ya margin kam pada")
    if d["sarthi_calls"]:
        add(1, f"{s} ke {d['sarthi_calls']} ideas accept kiye ({_when_hi(d['sarthi_days'])})",
            ("Puchho kaunsa idea pasand aaya. Next idea lene ke liye account mein funds chahiye" if d["unfunded"]
             else f"Aaj ke {s} ideas saath mein dekh lo, ek idea lene mein help karo"))
    if d["segments"]:
        add(2, f"{', '.join(d['segments'])} research {d['research_views']} baar dekha",
            f"{seg} mein interest hai, toh {s} {seg} ideas ki baat karo: entry, target, stop-loss sab ready")
    if d["record"]:
        add(2, d["record_hi"], "Facts batao, guarantee nahi. Zaroor bolo: \"past results future ki guarantee nahi hain\"")
    if d.get("example"):
        add(4, f"Recent {s} example: {d['example']}", "Example ki tarah batao, promise ki tarah nahi")

    # --- Sarthi activation (needed to receive the ideas)
    if stage != "payout":
        if d["sarthi_active"] is False:
            add(1, f"{s} activate nahi hai", f"Pehle {s} on karwao, ideas tabhi aayenge. Account ke saath included hai")
        elif d["sarthi_active"] is None:
            add(3, f"{s} status pata nahi", f"Check karo {s} on hai ya nahi; nahi hai toh call pe hi activate karwao")

    # --- where they are in their journey
    if stage == "kyc_no_deposit" and d["reg_days"] is not None:
        if d["reg_days"] <= 2:
            add(2, f"{'Aaj' if d['reg_days'] == 0 else str(d['reg_days']) + ' din pehle'} register kiya, deposit abhi nahi",
                "Welcome call ki tarah baat karo, setup mein help offer karo")
        elif d["reg_days"] > 7:
            add(3, f"Register kiye {d['reg_days']} din ho gaye, deposit nahi",
                f"Puchho kya roka. Agar 'kya trade karun' samajh nahi aata, toh {s} ready ideas deta hai")
    if stage == "dormant_trader" and d["ltd_days"] is not None:
        add(2, f"Last trade {_gap_hi(d['ltd_days'])} pehle; ab app mein wapas active",
            f"Puchho trading kyun band ki (charges, loss, time). {s} ideas se dobara shuru karwa sakte ho")
    if stage == "funded_no_trade":
        dep = f"₹{d['first_deposit']:,.0f} " if d.get("first_deposit") else ""
        add(2, f"{dep}deposit kiya hai, par trade abhi tak nahi", f"Pehla trade {s} ke kisi idea se karwao, saath mein")
    if stage == "low_margin":
        goal = d.get("target") or 0
        have = d.get("current_margin")
        m = d.get("margin")
        unlock = (f"; {s} {seg} idea ka typical margin ₹{m['median_margin']:,.0f}, toh ₹{goal:,} mein ~{int(goal // m['median_margin'])} ideas"
                  if m and not m.get("per_share_margin") and m.get("median_margin") else "")
        if have and goal and have < goal:
            add(0, f"Account mein abhi ~₹{have:,.0f} (target ₹{goal:,}){unlock}",
                f"₹{goal - have:,.0f} aur add karwao, ₹{goal:,} pe account properly funded hoga aur zyada ideas le sakenge")
        else:
            add(0, f"Low margin client: target ₹{goal:,}{unlock}", f"Top-up karwao ₹{goal:,} tak; UPI se 2 minute")
    if (d["unfunded"] or d.get("funding_push") or stage in {"dormant_trader", "funded_no_trade"}) and stage not in {"payout", "low_margin", "kyc_pending"}:
        goal = d.get("target") or 0
        ask = f"₹{goal:,}" if goal else d["starter_hi"]
        m = d.get("margin")
        first = d.get("first_deposit")
        if first and goal and first < goal and stage in {"dormant_trader", "funded_no_trade"}:
            add(1, f"Pehla deposit ₹{first:,.0f} tha; target ₹{goal:,}",
                f"₹{goal - first:,.0f} aur add karke ₹{goal:,} tak le jao, taaki {s} ke ideas aaram se le sakein")
        elif m and not m.get("per_share_margin"):
            fit = int(goal // m["median_margin"]) if goal and m["median_margin"] else 0
            cover = f"; last 30 din ke {m['share_within_target']:.0%} ideas {ask} mein aa jaate" if m.get("share_within_target") is not None else ""
            add(1, f"{s} {seg} idea ka typical margin ₹{m['median_margin']:,.0f}{cover}",
                f"{ask} add karwao: " + (f"ek saath ~{fit} ideas le sakte hain" if fit >= 2 else "ek idea aaram se le sakte hain")
                + " (risk ke saath, faisla unka)")
        elif m:
            add(1, f"Last 30 din mein {s} ne {m['ideas']} {seg or 'equity'} ideas diye; quantity customer khud choose karta hai",
                f"{ask} add karwao: kai ideas mein thoda-thoda le sakte hain, ek pe saara paisa nahi")
        else:
            add(1, f"Goal: {ask} deposit (funded account)", f"{ask} add karwao taaki {s} ke ideas le sakein; UPI se 2 minute")

    # --- context that helps the call land
    if d["last24"] >= 3:
        add(2, f"Last 24 ghante mein app pe {d['last24']} actions, abhi active hain", "Aaj hi call karo, kal tak mat ruko")
    if d["best_time"]:
        add(5, f"Usually {d['best_time']} app use karte hain", "Usi time call karna best rahega")
    if d["high_value"]:
        add(4, f"Holdings ₹{d['holdings']:,.0f}", f"High-value customer: {d.get('rm_hi') or 'dedicated RM'} offer karo")
    elif d.get("offer_rm") and d.get("rm_hi") and stage != "payout":
        add(5, "Trades manage karna mushkil lag sakta hai", f"Offer karo: {d['rm_hi']}")
    if d["npc_count"] >= 3:
        add(4, f"{d['npc_count']} baar phone nahi uthaya", "Best time pe try karo, ya pehle SMS / WhatsApp bhejo")
    if d.get("pricing_hi") and stage in {"kyc_no_deposit", "funded_no_trade", "dormant_trader"}:
        add(5, "Charges ka sawaal aa sakta hai", d["pricing_hi"])
    if d.get("market"):
        funding_call = (d["unfunded"] or d.get("funding_push") or stage in {"dormant_trader", "funded_no_trade", "low_margin"}) and stage != "payout"
        add(3 if funding_call else 6, f"Market: {d['market']}",
            ("Market ka mood sirf context ke liye batao (fact, tip nahi); funds ki baat client ke apne goal se karo" if funding_call
             else "Baat shuru karne ke liye zikr kar sakte ho (tip nahi, sirf fact)"))
    if d.get("reactivation_engaged"):
        add(1, "Re-activation list: app pe phir se active", "Yahi sahi time hai, aaj hi call karo")
    if not out:
        add(5, "App activity nahi dikhi", "Puchho login ya app mein koi problem toh nahi")

    out.sort(key=lambda x: x[0])
    seen, ideas = set(), []
    for _, fact, angle in out:
        if fact not in seen:
            seen.add(fact)
            ideas.append({"fact": fact, "angle": angle})
    return ideas[:6]


def propositions(cfg: dict, stage: str, seg: str | None, already: str) -> list[str]:
    """Swastika propositions that suit this customer (from config), skipping ones the ideas already cover."""
    out = []
    for p in cfg.get("propositions") or []:
        stages = set(p.get("for") or ["all"])
        segs = set(p.get("segments") or [])
        if ("all" in stages or stage in stages) and (not segs or seg in segs):
            pitch = str(p.get("pitch_hi") or "").strip()
            if pitch and pitch[:25].lower() not in already.lower():
                out.append(f"{p['name']}: {pitch}")
    return out[:4]


def closing(d: dict, cfg: dict) -> list[str]:
    """Assertive, straight close: ask for the full target, then a smaller first step with a booked top-up.
    No false urgency, and never on a payout."""
    s, stage = d["sname"], d["stage"]
    goal = d.get("target") or 0
    if stage == "payout":
        return [f"Wajah solve hone ke baad poochho: \"Payout normal process hoga. Chaahein toh thoda balance {s} ideas ke liye "
                "account mein rakh sakte hain. Aapki marzi.\"",
                "Na bole toh payout normal hone do, aur poochho: \"Agle mahine phir se ek baar baat karein?\" (follow-up set karo)"]
    if stage == "low_margin":
        goal = d.get("target") or 0
        have = d.get("current_margin") or 0
        gap = max(goal - have, 0) if goal else 0
        lines = [f"Seedha poochho: \"₹{gap:,.0f} abhi UPI se add karke ₹{goal:,} kar dete hain?\"" if gap
                 else f"Seedha poochho: \"Top-up karke ₹{goal:,} kar dete hain?\""]
        if gap > int((cfg.get("closing") or {}).get("fallback_amount") or 10000):
            lines.append(f"Agar poora nahi: \"Aaj ₹{gap // 2:,.0f}, baaki kab?\" Woh time follow-up mein set karo")
        lines.append("Haan bole toh wahin UPI karwao aur 💰 Converted pe top-up amount daalo")
        return lines
    if stage == "sarthi_activation":
        return [f"Seedha poochho: \"Abhi call pe {s} on kar dete hain? 1 minute lagega, aur aaj ke ideas saath mein dekh lenge.\"",
                "On ho gaya toh ⚡ Activated pe click karo. Abhi time nahi toh kal ka time le lo (✅ Will do it later)",
                "Na bole toh wajah note karo; dobara pressure nahi"]
    if stage == "referral":
        reward = f" {d['reward_hi'][0].upper()}{d['reward_hi'][1:]}." if d.get("reward_hi") else ""
        if d.get("referral_cohort") == "C1b":
            return ["Pehle reconnect: \"Aapne pehle dost refer kiye the, shukriya. Kuch time se trade nahi dikha: sab theek? Kya wapas "
                    "laa sakta hai?\"",
                    "Baat achhi chale tabhi: \"Koi aur dost jo trading shuru karna chahte hain? Unki permission se naam aur number.\""
                    + reward,
                    "Trade wapas shuru karne mein interest ho toh woh pehle: referral agle call pe (✅ Will share later)"]
        return [f"Seedha poochho: \"Koi 1-2 dost ya family jo trading karte hain ya shuru karna chahte hain? Unka naam aur number "
                f"de sakte hain? Hum unse pehle permission lekar hi baat karenge.{reward}\"",
                "Naam / number mile toh 🤝 Gave referral(s) pe likho. Abhi yaad nahi toh kal ka time le lo (✅ Interested)",
                "Na bole toh shukriya bolo; dobara pressure nahi"]
    if stage == "kyc_pending":
        return ["Seedha poochho: \"KYC abhi call pe saath mein poora kar lete hain? 5 minute lagenge.\"",
                "KYC ho jaaye toh agla call funding ke liye set karo (🔁 Wants a call back)"]
    if not (d["unfunded"] or d.get("funding_push") or stage in {"dormant_trader", "funded_no_trade"}):
        return [f"Seedha poochho: \"Aaj {s} ka ek idea saath mein dekh lein?\""]
    ask = f"₹{goal:,}" if goal else d["starter_hi"]
    fallback = int((cfg.get("closing") or {}).get("fallback_amount") or 0)
    lines = [f"Seedha poochho, assume karke: \"{ask} abhi UPI se add kar dete hain? Main line pe hoon, 2 minute lagenge.\""]
    if d["sarthi_active"] is False:
        lines.insert(0, f"Pehle {s} on karwao (call pe hi), phir funds ki baat")
    if fallback and goal and fallback < goal:
        lines.append(f"Agar abhi {ask} nahi: \"₹{fallback:,} se shuru karte hain, baaki ₹{goal - fallback:,} kab tak?\" "
                     "Woh time follow-up mein set karo (🔁 Wants a call back)")
    lines.append("Haan bole toh wahin UPI karwao aur 💰 Converted pe amount daalo. Na bole toh wajah note karo")
    return lines


def objections(stage: str, s: str, pricing_hi: str, rm_hi: str, target: int = 0, reward_hi: str = "") -> list[str]:
    """Short Hinglish answers to the objections most likely on this call."""
    if stage == "sarthi_activation":
        return [f"\"Main khud trade karta hoon\" → Bilkul; {s} sirf ideas bhejta hai, trade aap hi chunoge. On karne se koi trade nahi hota",
                f"\"Kitna profit hoga?\" → Track record jaisa hai waisa batao; returns guaranteed nahi, har idea mein stop-loss hai",
                "\"Charges?\" → Sirf official charges batao, apni taraf se kuch promise mat karo" + (f"; {pricing_hi}" if pricing_hi else "")]
    if stage == "referral":
        return ["\"Kisi ko nahi jaanta\" → Koi baat nahi; kabhi koi trading ke baare mein pooche toh app link share kar dena",
                "\"Dost ka number dena theek nahi lagta\" → Bilkul; aap unse pooch lo, ya unhe hamara number de do",
                "\"Mujhe kya milega?\" → " + (f"{reward_hi[0].upper()}{reward_hi[1:]}; isse zyada kuch promise mat karo" if reward_hi
                                                  else "Sirf official referral programme ki baat karo; apni taraf se kuch promise mat karo")]
    if stage == "payout":
        lines = ["\"Paise ki zaroorat hai\" → Bilkul, payout normal process hoga. Chaahein toh thoda balance "
                 f"{s} ideas ke liye rakh sakte hain"]
        if pricing_hi:
            lines.append(f"\"Zerodha / Groww pe ja raha hoon\" → {pricing_hi}" + (f"; aur {rm_hi}" if rm_hi else ""))
        lines.append(f"\"Loss ho gaya\" → {s} ideas stop-loss ke saath aate hain; chhote se shuru karo, returns guaranteed nahi")
        return lines
    lines = []
    if stage in {"kyc_no_deposit", "funded_no_trade", "dormant_trader", "kyc_pending"}:
        lines.append(f"\"Kya trade karun, pata nahi\" → Isi ke liye {s} hai: stop-loss ke saath ready ideas; "
                     "chhote amount se shuru karo, returns guaranteed nahi")
    if stage == "low_margin":
        goal_txt = f"₹{target:,}" if target else "target"
        lines.append(f"\"Pehle se paise daale hain\" → Haan, account live hai; {goal_txt} tak top-up se ek saath zyada {s} ideas le sakenge")
    if pricing_hi and stage != "active_trader":
        lines.append(f"\"Charges zyada hain\" / \"Zerodha ya Groww use karta hoon\" → {pricing_hi}")
    if rm_hi and stage in {"dormant_trader", "funded_no_trade", "active_trader"}:
        lines.append(f"\"Trades manage karne ka time nahi\" → Aapko {rm_hi}")
    lines.append({"kyc_pending": "\"Baad mein karunga\" → KYC abhi call pe saath mein 5 minute mein ho jaayega",
                  "kyc_no_deposit": "\"Baad mein paise daalunga\" → Abhi chhota amount UPI se daal do; kabhi bhi nikaal sakte ho",
                  "funded_no_trade": f"\"Kya khareedun?\" → Aaj ke {s} ideas dekho; faisla aapka",
                  "dormant_trader": "\"App / charges ki problem thi\" → Problem note karo, SM ko batao; ab kya badla hai woh dikhao",
                  "active_trader": "\"Abhi theek hai\" → Feedback lo; jo segment use nahi karte (risk disclosure ke saath) uski baat karo",
                  }.get(stage, ""))
    return [l for l in lines if l][:3]
