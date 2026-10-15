"""A simple call script for GMs who find pitching hard: one insight, a short numbered script, and the ask.

Three outputs per client, in plain Hinglish:
    insight  one line: what the client did in the app + what is missing (the reason to call)
    script   7 numbered steps to read out: namaste → insight question → listen → benefit → ask → objection → compliance
    ask      the call to action (funding target, Rs 30,000 by default) with the fallback and what to do on yes / no

Built from facts customer_insight() already has, so it never names a security or promises returns.
"""
from __future__ import annotations

SEG_PITCH = {"Equity": "shares ke ideas", "F&O": "F&O ideas (stop-loss ke saath)", "Commodity": "Gold, Crude jaise commodity ideas"}
COMPLIANCE = 'Yaad rakhein: "Market risk hai, returns guaranteed nahi hain."'
FUNDED_STAGES = {"funded_no_trade", "low_margin", "active_trader", "dormant_trader"}


def _rs(amount) -> str:
    return f"₹{int(amount):,}"


def _gap(days) -> str:
    if days is None:
        return ""
    return f"{days // 365} saal" if days >= 365 else f"{days // 30} mahine" if days >= 60 else f"{days} din"


def insight(f: dict) -> str:
    """Key insights, most important first: the gap this call closes, then the facts that make the pitch personal."""
    sname, seg, stage = f["sname"], f.get("seg"), f["stage"]
    have, target = f.get("current_margin"), f.get("target") or 0
    gap = {
        "kyc_pending": "KYC abhi poora nahi hua",
        "kyc_no_deposit": "Account khula hai par abhi tak paisa nahi daala"
                          + (f" ({f['reg_days']} din pehle register kiya)" if f.get("reg_days") is not None else ""),
        "funded_no_trade": "Paisa daala hai par pehla trade nahi hua",
        "low_margin": (f"Margin sirf {_rs(have)} hai, {_rs(target)} tak {_rs(target - have)} aur chahiye" if have and have < target
                       else "Margin kam hai, poore ideas nahi le paa rahe"),
        "dormant_trader": "Kaafi time se trade nahi kiya" + (f" (last trade {_gap(f.get('ltd_days'))} pehle)" if f.get("ltd_days") else ""),
        "payout": (f"{_rs(f['payout_amount'])} ka payout maanga hai" if f.get("payout_amount") else "Payout maanga hai")
                  + ": thoda balance rakh kar ideas lete reh sakte hain",
        "active_trader": "Regular trade karte hain: zyada balance se zyada ideas le sakte hain",
        "referral": "Bahut active trader hain: referral maangne ka sahi time",
        "sarthi_activation": f"{sname} abhi switch on nahi hai",
    }.get(stage)
    facts = []
    if f.get("orders_unfunded"):
        facts.append(f"{f['orders_unfunded']} order lagane ki koshish ki, par funds na hone se nahi gaya")
    if f.get("deposit_failed"):
        facts.append("Deposit try kiya tha par fail hua: UPI saath mein karwayein")
    if f.get("sarthi_calls"):
        facts.append(f"{sname} ke {f['sarthi_calls']} idea accept kiye")
    if seg:
        views = f.get("research_views") or 0
        facts.append(f"Research ideas {views} baar dekhe, sabse zyada {seg}" if views > 1 else f"{seg} mein interest")
    if (f.get("last24") or 0) >= 3:
        facts.append(f"Last 24 ghante mein app pe {f['last24']} baar active: aaj hi call karein")
    if f.get("sarthi_active") is False and stage not in {"sarthi_activation", "payout", "referral"}:
        facts.append(f"{sname} off hai: call pe on karwayein")
    if f.get("high_value") and f.get("holdings"):
        facts.append(f"High-value: holdings {_rs(f['holdings'])}")
    if not facts and not gap:
        facts.append("App pe registered hain" if stage in {"kyc_pending", "kyc_no_deposit"} else "Humare client hain")
    return "\n".join(f"• {line}" for line in ([gap] if gap else []) + facts[:4])


def _hook(f: dict) -> str:
    sname, seg, stage = f["sname"], f.get("seg"), f["stage"]
    if stage == "referral":
        return "Aap humare saath bahut active trade karte hain, thank you! Ab tak ka experience kaisa raha?"
    if stage == "payout":
        return "Aapne payout request ki hai, woh normal process mein ho jayega. Bas ek minute: sab theek hai account mein?"
    if f.get("orders_unfunded"):
        return "Maine dekha aapne app mein order lagane ki koshish ki thi, par funds na hone se order nahi gaya. Kaunsa trade karna tha?"
    if f.get("sarthi_calls"):
        return (f"Maine dekha aapne {sname} ke {f['sarthi_calls']} idea accept kiye" + (f" aur {seg} ideas dekh rahe hain" if seg else "")
                + ". Kaunsa idea sabse achha laga?")
    if seg:
        return f"Maine dekha aap app pe {seg} ideas dekh rahe hain. {seg} mein kya pasand hai, short term ya long term?"
    if stage == "dormant_trader":
        return "Aap pehle humare saath trade karte the. Kuch time se trade nahi hua, sab theek hai? Kya roka?"
    if stage == "sarthi_activation":
        return f"Aap regular trade karte hain. Kya aapne {sname} try kiya? Woh ready trade ideas deta hai."
    return "Aapne account khola hai, bahut achha. Kya aapne abhi tak koi trade try kiya?"


def _benefit(f: dict) -> str:
    sname, stage = f["sname"], f["stage"]
    pitch = SEG_PITCH.get(f.get("seg"), "ready trading ideas")
    target = _rs(f["target"])
    lines = {
        "kyc_pending": f"KYC 5 minute mein poora ho jayega. Phir {sname} aapko {pitch} dega: entry, target, stop-loss sab ready.",
        "kyc_no_deposit": f"{sname} aapko {pitch} deta hai: entry, target, stop-loss sab ready. Bas account mein funds chahiye, taaki agla idea le sakein.",
        "funded_no_trade": f"Aapka account ready hai. Pehle trade ke liye {sname} ke {pitch} le sakte hain. Main step by step bata deta hoon.",
        "low_margin": f"Abhi margin kam hai, isliye {sname} ke kai {pitch} miss ho rahe hain. {target} balance se ek saath 2-3 ideas le sakte hain.",
        "dormant_trader": f"Ab app mein {sname} aa gaya hai: {pitch}, roz. Aur pricing Zerodha / Groww jaisi discount wali hai.",
        "payout": f"Agar thoda balance account mein rakhein, toh {sname} ke {pitch} lete reh sakte hain. Paisa kabhi bhi nikaal sakte hain.",
        "active_trader": f"Aap regular trade karte hain. {target} balance se {sname} ke zyada {pitch} ek saath le sakte hain.",
        "referral": "Aapke dost ya family jo trade karte hain, unhe bhi discount pricing aur ready ideas milenge.",
        "sarthi_activation": f"{sname} on karne se koi trade nahi lagta, sirf {pitch} app mein aate hain. Aap decide karte hain kaunsa lena hai.",
    }
    line = lines.get(stage, f"{sname} aapko {pitch} deta hai: entry, target, stop-loss sab ready.")
    if f.get("sarthi_active") is False and stage not in {"payout", "referral", "sarthi_activation"}:
        line += f" Pehle {sname} switch on kar dete hain, taaki ideas aapke paas aayein."
    return line


def ask(f: dict) -> str:
    stage, target, sname = f["stage"], f["target"], f["sname"]
    t = _rs(target)
    if stage == "referral":
        reward = f" ({f['reward_hi']})" if f.get("reward_hi") else ""
        return ('"Koi dost ya family member hai jo trade karta hai ya shuru karna chahta hai? Unka naam aur number de dijiye, '
                f'main unse baat kar leta hoon."{reward}\nNa bole: thank you bolein aur feedback note karein. Number sirf dost ki marzi se lein.')
    if stage == "sarthi_activation":
        return (f'"Main line pe hoon, abhi {sname} on kar dete hain? 1 minute lagega."\n'
                f"Na bole: \"Koi baat nahi, kal ka ek idea dekh kar bataiye.\" Callback set karein.\nHaan bole: app mein saath mein on karwayein.")
    if stage == "payout":
        return (f'"Poora payout lene ki jagah {t} account mein rakh dete hain? Agle {sname} idea ke liye ready rahega."\n'
                f'Na bole: "Theek hai, {_rs(min(10000, target))} hi rakh lijiye." Phir bhi na: payout hone dein, wajah note karein.')
    if stage == "kyc_pending":
        return (f'"Abhi saath mein KYC poora kar lete hain? Phir {t} UPI se add karke pehla idea le sakte hain."\n'
                "Na bole: KYC ke liye time poochhein aur callback set karein.")
    have = f.get("current_margin")
    if stage == "low_margin" and have and have < target:
        gap = target - have
        first = f'"Abhi {_rs(have)} hai. {_rs(gap)} aur add karke {t} kar dete hain? UPI se 2 minute."'
        half = max(1000, gap // 2 // 1000 * 1000)
        no = (f'"Kab tak {_rs(gap)} add kar payenge?"' if half >= gap else
              f'"Chaliye aaj {_rs(half)} add kar dete hain, baaki {_rs(gap - half)} kab tak?"')
        return (first + f"\nNa bole: {no} Woh date callback mein set karein."
                "\nHaan bole: wahin UPI karwayein, phir 💰 Converted pe amount daalein.")
    if stage in FUNDED_STAGES:
        first = f'"{t} tak top-up abhi UPI se kar dete hain? Main line pe hoon, 2 minute lagenge."'
    else:
        first = f'"{t} abhi UPI se add kar dete hain? Main line pe hoon, 2 minute lagenge."'
    start = min(10000, target)
    rest = f", baaki {_rs(target - start)} kab tak?" if target > start else "?"
    return (first + f"\nNa bole: \"Chaliye {_rs(start)} se shuru karte hain{rest}\" Woh date callback mein set karein."
            "\nHaan bole: wahin UPI karwayein, phir 💰 Converted pe amount daalein.")


def script(f: dict) -> str:
    when = f" (best time: {f['best_time']})" if f.get("best_time") else ""
    return "\n".join([
        f"1. Namaste: \"Namaste, main [aapka naam] Swastika se bol raha/rahi hoon. 2 minute baat kar sakte hain?\"{when}",
        f"2. Insight: \"{_hook(f)}\"",
        "3. Suniye: client ko bolne dein. Unka jawab note karein.",
        f"4. Fayda: \"{_benefit(f)}\"",
        f"5. Ask: {ask(f).splitlines()[0]}",
        "6. Na bole toh: \"Kya cheez rok rahi hai?\" Jawab 'Agar customer bole' se dein.",
        f"7. {COMPLIANCE}",
    ])


def build(f: dict) -> dict:
    """f: sname, stage, seg, target (int) + optional facts (see customer_insight)."""
    f = {**f, "target": f.get("target") or 30000}
    return {"script_insight": insight(f), "script_text": script(f), "script_ask": ask(f)}
