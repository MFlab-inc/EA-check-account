#!/usr/bin/env python3
"""
EA-check-account: Myfxbook巡回によるEA稼働確認
- 各Myfxbookアカウント(investarN)にAPIログイン
- get-my-accounts で全登録口座を収集
- 更新停止 / 無取引期間 / ポジション不変 / equity乖離 を判定
- data/status.json と docs/ 用データを出力

Secrets:
  MYFXBOOK_CREDENTIALS: JSON文字列
    {"investar1": {"email": "...", "password": "..."},
     "investar17": {"email": "...", "password": "..."}, ...}
"""
import json
import os
import sys
import time
import hashlib
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

JST = timezone(timedelta(hours=9))
API_BASE = "https://www.myfxbook.com/api"
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATUS_PATH = DATA_DIR / "status.json"
CONFIG_PATH = ROOT / "config" / "thresholds.yaml"

UA = "EA-check-account/1.0 (github-actions; monitoring own accounts)"


def api_get(endpoint: str, params: dict, retries: int = 3) -> dict:
    qs = urllib.parse.urlencode(params)
    url = f"{API_BASE}/{endpoint}?{qs}"
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
            if data.get("error"):
                raise RuntimeError(f"API error: {data.get('message', 'unknown')}")
            return data
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{endpoint} failed after {retries} tries: {last_err}")


def parse_mfb_date(s: str):
    """Myfxbook日時 'MM/dd/yyyy HH:mm' → aware datetime (UTC扱い)"""
    if not s:
        return None
    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def load_thresholds() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_retired() -> set:
    """config/retired.yaml の運用終了口座名リスト"""
    path = ROOT / "config" / "retired.yaml"
    if not path.exists():
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return set(data.get("retired") or [])
    except Exception as e:  # noqa: BLE001
        print(f"retired.yaml 読み込み失敗（無視して続行）: {e}", file=sys.stderr)
        return set()


def load_previous_status() -> dict:
    """前回のstatus.json（open_trades_hashの継続日数計算に使用）"""
    if STATUS_PATH.exists():
        try:
            with open(STATUS_PATH, encoding="utf-8") as f:
                prev = json.load(f)
            return {a["myfxbook_oid"]: a for a in prev.get("accounts", [])}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def open_trades_signature(trades: list) -> str:
    """オープンポジション構成のハッシュ（チケット順序非依存）"""
    keys = sorted(
        f"{t.get('openTime','')}|{t.get('symbol','')}|{t.get('action','')}|{t.get('sizing',{}).get('value','')}"
        for t in trades
    )
    return hashlib.sha1("\n".join(keys).encode("utf-8")).hexdigest()[:16]


def evaluate(acc: dict, open_trades: list, last_closed: str | None,
             th: dict, prev: dict, now_utc: datetime,
             retired_names: set | None = None) -> dict:
    reasons = []
    level = "OK"  # OK < WATCH < WARN < ALERT

    def bump(new_level: str, reason: str):
        nonlocal level
        order = {"OK": 0, "WATCH": 1, "WARN": 2, "ALERT": 3}
        if order[new_level] > order[level]:
            level = new_level
        reasons.append(reason)

    # 1) 更新停止（接続断・ターミナル停止の疑い）
    last_update = parse_mfb_date(acc.get("lastUpdateDate", ""))
    hours_since_update = None
    if last_update:
        hours_since_update = (now_utc - last_update).total_seconds() / 3600
        if hours_since_update >= th["update_stale_alert_hours"]:
            bump("ALERT", f"Myfxbook更新が{hours_since_update:.0f}時間停止（接続断疑い）")
        elif hours_since_update >= th["update_stale_warn_hours"]:
            bump("WARN", f"Myfxbook更新が{hours_since_update:.0f}時間停止")
    else:
        bump("WARN", "lastUpdateDateが取得できない")

    # 2) 無取引期間（EA停止疑い）
    days_since_trade = None
    lc = parse_mfb_date(last_closed) if last_closed else None
    lo = None
    if open_trades:
        lo_dates = [parse_mfb_date(t.get("openTime", "")) for t in open_trades]
        lo_dates = [d for d in lo_dates if d]
        lo = max(lo_dates) if lo_dates else None
    last_activity = max([d for d in (lc, lo) if d], default=None)
    if last_activity:
        # Myfxbookのタイムスタンプはブローカー時間（UTCより先行し得る）のため
        # マイナスになる場合は0に丸める
        days_since_trade = max(0, (now_utc - last_activity).days)
        if days_since_trade >= th["no_trade_alert_days"]:
            bump("WARN", f"最終取引アクティビティから{days_since_trade}日経過（EA停止疑い）")
        elif days_since_trade >= th["no_trade_watch_days"]:
            bump("WATCH", f"最終取引アクティビティから{days_since_trade}日経過")

    # 3) オープンポジション構成の長期不変（グリッド系の異常シグナル）
    sig = open_trades_signature(open_trades) if open_trades else ""
    sig_since = now_utc.isoformat()
    p = prev.get(acc["id"]) or prev.get(str(acc["id"])) or {}
    if sig and p.get("open_trades_hash") == sig and p.get("open_trades_hash_since"):
        sig_since = p["open_trades_hash_since"]
        try:
            since_dt = datetime.fromisoformat(sig_since)
            unchanged_days = (now_utc - since_dt).days
            if unchanged_days >= th["positions_unchanged_watch_days"]:
                bump("WATCH", f"オープンポジション構成が{unchanged_days}日間不変")
        except ValueError:
            pass

    # 4) balance/equity乖離（浮動損の急拡大。稼働確認とは別軸の早期警戒）
    balance = acc.get("balance")
    equity = acc.get("equity")
    float_dd_pct = None
    if isinstance(balance, (int, float)) and isinstance(equity, (int, float)) and balance:
        float_dd_pct = round((balance - equity) / balance * 100, 2)
        if float_dd_pct >= th["floating_dd_warn_pct"]:
            bump("WARN", f"浮動DDが残高比{float_dd_pct:.1f}%")

    # 5) 分類の上書き（優先度: RETIRED > STOPPED > 通常判定）
    # RETIRED: config/retired.yaml に明示された運用終了口座
    if retired_names and (acc.get("name") in retired_names):
        level = "RETIRED"
        reasons = ["運用終了（retired.yaml指定）"]
    # STOPPED: 更新停止が30日(設定値)を超える長期停止。直近の異変(ALERT)と区別する
    elif hours_since_update is not None and \
            hours_since_update >= th.get("update_stopped_days", 30) * 24:
        level = "STOPPED"
        reasons = [f"Myfxbook更新が{hours_since_update / 24:.0f}日停止（長期停止）"]

    return {
        "level": level,
        "reasons": reasons,
        "hours_since_update": round(hours_since_update, 1) if hours_since_update is not None else None,
        "days_since_trade": days_since_trade,
        "open_trades_hash": sig,
        "open_trades_hash_since": sig_since if sig else None,
        "float_dd_pct": float_dd_pct,
    }


def main():
    creds_raw = os.environ.get("MYFXBOOK_CREDENTIALS", "")
    if not creds_raw:
        print("ERROR: MYFXBOOK_CREDENTIALS が未設定", file=sys.stderr)
        sys.exit(1)
    credentials = json.loads(creds_raw)

    th = load_thresholds()
    retired_names = load_retired()
    prev = load_previous_status()
    now_utc = datetime.now(timezone.utc)
    results = []
    login_errors = []

    for mfb_name, cred in credentials.items():
        session = None
        try:
            login = api_get("login.json", {"email": cred["email"], "password": cred["password"]})
            # Myfxbookはセッションを既にURLエンコード済み（%2B等を含む）で返すため、
            # 一度デコードしてから使う。そのまま使うとurlencodeで%→%25に二重変換され
            # 以降の呼び出しが全てInvalid sessionになる。
            session = urllib.parse.unquote(login["session"])
            accounts = api_get("get-my-accounts.json", {"session": session}).get("accounts", [])
            print(f"[{mfb_name}] {len(accounts)} accounts")

            for acc in accounts:
                oid = acc["id"]
                open_trades = []
                last_closed = None
                try:
                    open_trades = api_get(
                        "get-open-trades.json", {"session": session, "id": oid}
                    ).get("openTrades", []) or []
                except Exception as e:  # noqa: BLE001
                    print(f"  open-trades failed for {acc.get('name')}: {e}", file=sys.stderr)
                try:
                    hist = api_get(
                        "get-history.json", {"session": session, "id": oid}
                    ).get("history", []) or []
                    closed = [h for h in hist if h.get("closeTime")]
                    if closed:
                        closed_dt = [(parse_mfb_date(h["closeTime"]), h) for h in closed]
                        closed_dt = [(d, h) for d, h in closed_dt if d]
                        if closed_dt:
                            last_closed = max(closed_dt, key=lambda x: x[0])[1]["closeTime"]
                except Exception as e:  # noqa: BLE001
                    print(f"  history failed for {acc.get('name')}: {e}", file=sys.stderr)

                ev = evaluate(acc, open_trades, last_closed, th, prev, now_utc,
                              retired_names=retired_names)
                # 公開マスク版: 口座番号(accountId)・残高・エクイティ・損益額・gain・drawdownは
                # リポジトリがPublicのため出力しない。比率(float_dd_pct)と稼働指標のみ。
                results.append({
                    "myfxbook_login": mfb_name,
                    "myfxbook_oid": oid,  # Myfxbook内部ID(公開プロフィールURLと同一情報)
                    "name": acc.get("name"),
                    "demo": acc.get("demo"),
                    "last_update": acc.get("lastUpdateDate"),
                    "last_closed_trade": last_closed,
                    "open_trades_count": len(open_trades),
                    **ev,
                })
                time.sleep(th.get("per_account_sleep_sec", 2))
        except Exception as e:  # noqa: BLE001
            login_errors.append({"myfxbook_login": mfb_name, "error": str(e)})
            print(f"[{mfb_name}] FAILED: {e}", file=sys.stderr)
        finally:
            if session:
                try:
                    api_get("logout.json", {"session": session})
                except Exception:  # noqa: BLE001
                    pass
        time.sleep(th.get("per_login_sleep_sec", 3))

    order = {"ALERT": 0, "WARN": 1, "WATCH": 2, "OK": 3, "STOPPED": 4, "RETIRED": 5}
    results.sort(key=lambda r: (order.get(r["level"], 9), r.get("name") or ""))

    summary = {
        "generated_at_jst": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "generated_at_utc": now_utc.isoformat(),
        "total_accounts": len(results),
        "counts": {lv: sum(1 for r in results if r["level"] == lv)
                   for lv in ("ALERT", "WARN", "WATCH", "OK", "STOPPED", "RETIRED")},
        "login_errors": login_errors,
        "thresholds": th,
        "accounts": results,
    }

    DATA_DIR.mkdir(exist_ok=True)
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"written: {STATUS_PATH}")
    print(json.dumps(summary["counts"], ensure_ascii=False))

    # ジョブサマリー（Actions画面用）
    gh_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_summary:
        with open(gh_summary, "a", encoding="utf-8") as f:
            f.write(f"## EA稼働確認 {summary['generated_at_jst']}\n\n")
            c = summary["counts"]
            f.write(f"ALERT: {c['ALERT']} / WARN: {c['WARN']} / "
                    f"WATCH: {c['WATCH']} / OK: {c['OK']} / "
                    f"長期停止: {c['STOPPED']} / 運用終了: {c['RETIRED']}\n\n")
            bad = [r for r in results if r["level"] in ("ALERT", "WARN", "WATCH")]
            if bad:
                f.write("| Level | 口座 | 理由 |\n|---|---|---|\n")
                for r in bad:
                    f.write(f"| {r['level']} | {r['name']} ({r['myfxbook_login']}) | "
                            f"{'; '.join(r['reasons'])} |\n")
            if login_errors:
                f.write("\n### ログイン失敗\n")
                for e in login_errors:
                    f.write(f"- {e['myfxbook_login']}: {e['error']}\n")


if __name__ == "__main__":
    main()
