"""规则树：把 rules/ 目录下的 ACL4SSR 片段拼成 config.yaml 的 rules: 区块。

四个动作：order 看顺序、fetch 从上游拉片段、diff 对比现网、apply 落地（备份 →
写 → mihomo -t 校验 → 失败回滚），另有 rollback 回到某个备份。写盘和校验的
公共部分（备份、mihomo -t、热重载）在 core.py。
"""
from __future__ import annotations

import argparse
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from core import (BACKUP_DIR, RESTART_HINT, bad, backup_config, config_path, die, dim,
                  fmt_ts, list_backups, ok, pad, reload_config, require_config,
                  size_str, validate_config, warn)

# ─────────────────────── rules 子命令 ───────────────────────
# 把 rules/ 目录下的片段拼成 mihomo 的 rules: 区块。
#
# 片段本身不带策略（ACL4SSR 的约定，好让同一份规则能被不同策略复用），
# 策略由它所在的目录决定，顺序由 order.txt 决定。

RULES_DIR = Path(__file__).resolve().parent / "rules"
ORDER_FILE = RULES_DIR / "order.txt"

# 目录名 → 策略。目录名就是"这批规则要往哪走"。
# 注意这些名字必须能在 config.yaml 里找到（组名或内建策略），mihomo 会校验。
POLICY_BY_DIR = {"proxy": "节点选择", "direct": "全球直连", "reject": "全球拦截"}

# 片段里出现在 payload 之后的字段是**参数**而不是策略，
# 拼策略时必须插在它们前面：IP-CIDR,1.2.3.0/24,no-resolve → ...,DIRECT,no-resolve
RULE_PARAMS = {"no-resolve", "src", "dport"}

# mihomo 1.19 实测支持的类型。不在表里的会被跳过并告警——
# 比如 URL-REGEX 是 Clash Premium 的，mihomo 会报 unsupported rule type，
# 后果是**整份配置加载失败**，不是"这一条失效"。
SUPPORTED_RULE_TYPES = {
    "DOMAIN", "DOMAIN-SUFFIX", "DOMAIN-KEYWORD", "DOMAIN-REGEX",
    "IP-CIDR", "IP-CIDR6", "IP-SUFFIX", "IP-ASN", "SRC-IP-CIDR",
    "GEOIP", "GEOSITE", "PROCESS-NAME", "PROCESS-PATH",
    "DST-PORT", "SRC-PORT", "NETWORK", "RULE-SET", "MATCH", "FINAL",
}

# ACL4SSR_Online_Full_AdblockPlus.ini 的规则集顺序，映射到本目录的片段。
#
# 顺序不是小事：先到先得，而 proxy/ 里有 DOMAIN-KEYWORD,google。
# 一旦把 proxy 放在 direct 前面，GoogleCN（29 条里 23 条）和
# GoogleFCM（44 条里 18 条）会被这个关键字全部吃掉——不报错，只是静默走错。
# [] 开头的是内联规则，语法拄 ACL4SSR 的 []GEOIP,CN。
CANONICAL_ORDER: list[tuple[str, str | None]] = [
    ("direct/LocalAreaNetwork.list", "全球直连"),
    ("reject/BanAD.list", "全球拦截"),
    ("reject/BanProgramAD.list", "全球拦截"),
    ("reject/BanEasyList.list", "全球拦截"),
    ("reject/BanEasyListChina.list", "全球拦截"),
    ("reject/BanEasyPrivacy.list", "全球拦截"),
    ("direct/GoogleFCM.list", "全球直连"),
    ("direct/GoogleCN.list", "全球直连"),
    ("direct/Apple.list", "全球直连"),
    ("proxy/Telegram.list", "节点选择"),
    ("direct/ChinaMedia.list", "全球直连"),
    ("proxy/ProxyMedia.list", "节点选择"),
    ("direct/ChinaIp.list", "全球直连"),
    ("direct/ChinaIpV6.list", "全球直连"),
    ("proxy/Custom.list", "节点选择"),
    ("proxy/ProxyGFWlist.list", "节点选择"),
    ("proxy/ProxyLite.list", "节点选择"),
    ("direct/Custom.list", "全球直连"),
    ("reject/Custom.list", "全球拦截"),
    ("direct/ChinaDomain.list", "全球直连"),
    ("direct/ChinaCompanyIp.list", "全球直连"),
    ("[]GEOIP,CN,全球直连", None),
    ("[]MATCH,漏网之鱼", None),
]


def fragment_rules(path: Path) -> list[str]:
    """读一个片段里的规则行，跳过注释与空行。"""
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def read_order() -> tuple[list[tuple[str, str | None]], str]:
    """读出片段顺序：返回 ([(片段, 策略)], 来源说明)。

    优先用 rules/order.txt；没有就用内置的 ACL4SSR 规范顺序。
    调用方会把"用的是哪一份"告诉用户，不静默。
    """
    if not ORDER_FILE.exists():
        return CANONICAL_ORDER, f"内置的 ACL4SSR 规范顺序（{ORDER_FILE.name} 不存在）"

    entries: list[tuple[str, str | None]] = []
    for line in ORDER_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[]"):                 # 内联规则，自带策略
            entries.append((line, None))
            continue
        bits = line.split()
        entries.append((bits[0], bits[1] if len(bits) > 1 else None))
    return entries, str(ORDER_FILE)


def with_policy(line: str, policy: str | None) -> str:
    """给片段里的规则补上策略。

    片段格式是 TYPE,payload[,参数…]，所以策略插在 payload 之后、参数之前。
    如果这条本来就自带策略（自定义片段里可能这么写），就尊重它自己的。
    """
    f = [x.strip() for x in line.split(",")]
    if f[0].upper() in ("MATCH", "FINAL"):        # 没有 payload
        return ",".join(f[:1] + ([policy] if policy else []) + f[1:])
    payload, rest = f[1] if len(f) > 1 else "", f[2:]
    if rest and rest[0] not in RULE_PARAMS:
        return ",".join(f)
    return ",".join([f[0], payload] + ([policy] if policy else []) + rest)


def build_rules(prune: bool = False) -> tuple[list[str], list[dict], str, dict]:
    """按顺序拼出完整的规则行。返回 (规则, 问题, 顺序来源, 统计)。

    去重是默认行为：同一个 (类型,值) 只保留**第一次**出现的那条。
    这不算是“删东西”——先到先得，后面那些重复本来就不生效，
    只是占内存、让日志和 diff 变浑。

    prune=True 时额外剔除被前面更宽规则遮蔽的条目（同样行为等价，
    依据是被剔除的规则能匹配的每一个域名都已被更早的规则拿走了）。
    """
    order, origin = read_order()
    kept, stats, problems = walk_order(order, prune)

    rules = [line if entry.startswith("[]") else with_policy(line, policy)
             for entry, policy, line in kept]
    total = {"n": sum(s["n"] for s in stats.values()),
             "dup": sum(s["dup"] for s in stats.values()),
             "shadow": sum(s["shadow"] for s in stats.values())}
    return rules, problems, origin, {"raw": total["n"], "duplicates": total["dup"],
                                     "shadowed": total["shadow"], "stats": stats}


def split_config(text: str) -> tuple[str, list[str], str]:
    """把 config.yaml 拆成 (rules: 之前, 现有规则, rules: 之后)。"""
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if l.startswith("rules:")), None)
    if start is None:
        die(f"{config_path()} 里找不到 rules: 区块")
    end = start + 1
    while end < len(lines) and (lines[end].startswith("- ") or not lines[end].strip()):
        end += 1
    head, tail = "".join(lines[:start + 1]), "".join(lines[end:])
    cur = [l[2:].strip() for l in lines[start + 1:end] if l.startswith("- ")]
    return head, cur, tail


def rule_key(line: str) -> tuple[str, str]:
    p = line.split(",")
    return (p[0].upper(), p[1].casefold() if len(p) > 1 else "")


def report_problems(problems: list[dict]) -> None:
    for p in problems:
        if p["kind"] == "missing":
            print(warn(f"  ⚠ 顺序表里列了、但文件不存在，已跳过：{p['entry']}"))
        elif p["kind"] == "unlisted":
            print(warn(f"  ⚠ 文件存在、但不在顺序表里，会被忽略：{p['entry']}"))
        elif p["kind"] == "no_policy":
            print(warn(f"  ⚠ 目录名推不出策略，已跳过：{p['entry']}"))
        elif p["kind"] == "unsupported":
            detail = "、".join(f"{t}×{n}" for t, n in sorted(p["types"].items()))
            print(warn(f"  ⚠ {p['entry']} 跳过 {sum(p['types'].values())} 条 "
                       f"mihomo 不支持的类型：{detail}"))


def cmd_rules_rollback(args: argparse.Namespace) -> int:
    cfg = require_config()
    items = list_backups()
    if not items:
        die(f"没有可用备份。找过这两个地方：\n    {BACKUP_DIR}\n    {cfg.parent}")

    print(dim("可用备份（新 → 旧）："))
    for i, (ts, p, src) in enumerate(items, 1):
        where = "状态目录" if src == BACKUP_DIR else "config 同级"
        mark = ok("← 默认") if i == 1 else ""
        print(f"  {i:>2}  {fmt_ts(ts):<28}  {size_str(p.stat().st_size):>9}"
              f"  {dim(where)}  {mark}")
    if args.list:
        return 0

    # 选哪个：--to 可以是序号，也可以是时间戳前缀
    if args.to is None:
        target = items[0]
    elif args.to.isdigit() and 1 <= int(args.to) <= len(items):
        target = items[int(args.to) - 1]
    else:
        hits = [x for x in items if x[0].startswith(args.to)]
        if len(hits) != 1:
            die(f"--to {args.to} 匹配到 {len(hits)} 个备份，写完整时间戳或序号")
        target = hits[0]

    ts, src_path, _ = target
    # 先把备份内容读进内存：万一后面任何东西覆盖了这个文件，恢复的仍是这份内容
    payload = src_path.read_bytes()
    # 回滚本身也要可撤销：先把当前配置另存一份（同时也受保留策略约束）
    keep = backup_config()
    cfg.write_bytes(payload)

    # 先校验再报成功：不然会先打一句“已回滚”，紧跟着又说“校验失败”
    good, last = validate_config()
    if not good:
        shutil.copy2(keep, cfg)                     # 回滚的回滚
        print(bad(f"✗ {fmt_ts(ts)} 这份备份没通过 mihomo -t，已退回回滚前的配置"))
        print(bad(f"  {last}"))
        print(dim(f"  回滚前的配置已存到 {keep}"))
        return 1

    print(f"{ok('✓')} 当前配置已另存 {dim(str(keep))}")
    print(f"{ok('✓')} 已回滚到 {fmt_ts(ts)} 的备份  {dim(size_str(len(payload)))}")
    print(f"{ok('✓')} mihomo -t 校验通过  {dim(last)}")

    if args.reload:
        if reload_config():
            print(f"{ok('✓')} 已热重载运行中的 mihomo")
        else:
            print(warn(f"⚠ 热重载失败，文件已写好，可以 {RESTART_HINT}"))
    else:
        print(dim("  没有热重载；加 --reload 让它立即生效"))
    return 0


# 片段的上游来源：树里的相对路径 → ACL4SSR 仓库里的路径。
# 注：GoogleCN/Apple/Telegram/ProxyGFWlist 在 ACL4SSR 里顶层和 Ruleset/ 下都有，
# 这里用的是顶层那份（实比 md5 确认过：树里的内容与顶层一致）。
# 只有 GoogleFCM 在 Ruleset/ 下。
UPSTREAM = {
    "direct/LocalAreaNetwork.list": "Clash/LocalAreaNetwork.list",
    "direct/GoogleFCM.list": "Clash/Ruleset/GoogleFCM.list",
    "direct/GoogleCN.list": "Clash/GoogleCN.list",
    "direct/Apple.list": "Clash/Apple.list",
    "direct/ChinaMedia.list": "Clash/ChinaMedia.list",
    "direct/ChinaIp.list": "Clash/ChinaIp.list",
    "direct/ChinaIpV6.list": "Clash/ChinaIpV6.list",
    "direct/ChinaDomain.list": "Clash/ChinaDomain.list",
    "direct/ChinaCompanyIp.list": "Clash/ChinaCompanyIp.list",
    "proxy/Telegram.list": "Clash/Telegram.list",
    "proxy/ProxyMedia.list": "Clash/ProxyMedia.list",
    "proxy/ProxyGFWlist.list": "Clash/ProxyGFWlist.list",
    "proxy/ProxyLite.list": "Clash/ProxyLite.list",
    "reject/BanAD.list": "Clash/BanAD.list",
    "reject/BanProgramAD.list": "Clash/BanProgramAD.list",
    "reject/BanEasyList.list": "Clash/BanEasyList.list",
    "reject/BanEasyListChina.list": "Clash/BanEasyListChina.list",
    "reject/BanEasyPrivacy.list": "Clash/BanEasyPrivacy.list",
}
# 先从 raw 拉，不通再退 CDN（raw.githubusercontent 在国内经常直接拿不到）
# 上游只取 raw.githubusercontent.com，不挂 CDN 退路：多一个第三方就多一个供应链面，
# 而实测走本机 mihomo 代理每个文件 0.5~1.3 秒，本来就走得通。
UPSTREAM_BASE = "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/"


def http_get(url: str, proxy: str | None, timeout: float = 30) -> bytes:
    """下载一个 URL。proxy 形如 http://127.0.0.1:7890，None 表示直连。

    超时给 30 秒：单个文件最大也就 1.4MB，实测走代理 1.3 秒。
    原来写 90 秒，一旦碰上网络停滞、再叠上两级退路，用户要自等三分钟。
    """
    handlers = [urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {})]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "mihomo-cli"})
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def cmd_rules_fetch(args: argparse.Namespace) -> int:
    """从 ACL4SSR 拉那 18 个片段。

    拉下来的就是**上游原文**，一个字不改：这样镜像片段能直接和上游 diff，
    “本地有没有偏离上游”一目了然。上游里那些 mihomo 不支持的类型
    （URL-REGEX）由构建阶段忽略并告警，不在文件层面动手。

    仓库里不入库这些第三方内容（GPL），所以新机器上 clone 完跑一次这个，
    再把东西装到配置目录就齐了。

    默认直连，不默默借本机 mihomo 的代理：一是 fetch 恰恰是配置/代理坏掉时
    才最需要跑的命令，再把代理绕进去就成了鸡生蛋；二是不想隐式换出口。
    真要过代理（比如服务器上 raw 被墙）就显式给 --proxy。
    """
    proxy = args.proxy or None
    print(dim(f"下载路线：{'走代理 ' + proxy if proxy else '直连'}"))
    print()

    added = updated = same = failed = 0
    for rel in sorted(UPSTREAM):
        dst = RULES_DIR / rel
        try:
            data = http_get(UPSTREAM_BASE + UPSTREAM[rel], proxy)
        except (urllib.error.URLError, OSError) as e:
            print(bad(f"  ✗ {rel}  下载失败：{e}"))
            failed += 1
            continue

        old = dst.read_bytes() if dst.exists() else None
        if old == data:
            print(dim(f"  = {rel}  已是最新"))
            same += 1
            continue
        if args.dry_run:
            state = "新增" if old is None else "会更新"
            print(warn(f"  ~ {rel}  {state}"))
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)                    # 上游原文，一字不改
        if old is None:
            print(f"  {ok('+')} {rel}  新增  {size_str(len(data))}")
            added += 1
        else:
            print(f"  {ok('~')} {rel}  更新  {size_str(len(old))} → {size_str(len(data))}")
            updated += 1

    print()
    tail = f"新增 {added} / 更新 {updated} / 未变 {same}"
    if failed:
        tail += f" / {bad(f'失败 {failed}')}"
    print(f"  {tail}")
    if args.dry_run:
        print(dim("  --dry-run：什么都没写。去掉它才会真的下载。"))
    else:
        print(dim("  接着跑 mihomo-cli rules diff 看差异，没问题再 apply"))
    return 1 if failed else 0


def cmd_rules(args: argparse.Namespace) -> int:
    action = getattr(args, "rules_action", None) or "diff"   # 不带则默认 diff，只读
    if not hasattr(args, "prune"):
        args.prune = False          # 没走子解析器时没有这个属性
    return {"order": cmd_rules_order, "diff": cmd_rules_diff, "fetch": cmd_rules_fetch,
            "apply": cmd_rules_apply, "rollback": cmd_rules_rollback}[action](args)


def shadow_reason(t: str, v: str, seen_kw: set[str], seen_sfx: set[str]) -> str | None:
    """这条规则会不会被前面某条更宽的规则吃掉？返回原因，否则 None。

    每条判据都必须保证“更早那条能匹配本条能匹配的一切”——否则就会把活规则
    误判成死规则删掉。实跈踩过的坑：按“键”而不是按“出现”删，
    把 recaptcha.net 的首个出现（GoogleCN→DIRECT）也删了，域名直接掉到 MATCH。

    只比域名空间；IP 网段相交判定贵得多，不在这里算（宁漏不错）。
    """
    if not v:
        return None
    parents = {".".join(v.split(".")[k:]) for k in range(len(v.split(".")))}
    if t == "DOMAIN":
        # 同值的 DOMAIN-SUFFIX 也能拿它，所以要带上 v 自己
        if hit := parents & seen_sfx:
            return f"被 DOMAIN-SUFFIX,{sorted(hit)[0]} 覆盖"
    elif t == "DOMAIN-SUFFIX":
        if hit := (parents - {v}) & seen_sfx:          # 排除自己那一层
            return f"被 DOMAIN-SUFFIX,{sorted(hit)[0]} 覆盖"
    elif t != "DOMAIN-KEYWORD":
        return None
    if t in ("DOMAIN", "DOMAIN-SUFFIX"):
        if k := next((k for k in seen_kw if k in v), None):
            return f"被 DOMAIN-KEYWORD,{k} 覆盖"
    else:                                              # DOMAIN-KEYWORD
        if k := next((k for k in seen_kw if k != v and k in v), None):
            return f"被 DOMAIN-KEYWORD,{k} 覆盖"
    return None


def walk_order(order: list[tuple[str, str | None]], prune: bool
               ) -> tuple[list[tuple[str, str | None, str]], dict[str, dict], list[dict]]:
    """按顺序扫一遍，算出哪些规则真正生效。

    一条规则不生效只有两种可能：
      1. 同一个 (类型,值) 在前面已经出现过——先到先得，后面那条永远不会被看到；
      2. 被前面某条更宽的规则遮蔽（见 shadow_reason）。

    关键：判断和删除都是**按出现**，不是按键；而且只有真正留下来的规则
    才能当“遮蔽源”（被删掉的规则不再遮蔽后面的）。

    返回 ([(片段, 策略, 规则行)], 每片段统计, 问题列表)。
    """
    seen_exact: set[tuple[str, str]] = set()
    seen_kw: set[str] = set()
    seen_sfx: set[str] = set()
    kept: list[tuple[str, str | None, str]] = []
    stats: dict[str, dict] = {}
    problems: list[dict] = []

    for entry, explicit in order:
        if entry.startswith("[]"):                     # 内联规则，自带策略
            kept.append((entry, None, entry[2:].strip()))
            continue

        frag = RULES_DIR / entry
        if not frag.exists():
            problems.append({"kind": "missing", "entry": entry})
            stats[entry] = {"n": 0, "dup": 0, "shadow": 0, "examples": []}
            continue
        policy = explicit or POLICY_BY_DIR.get(entry.split("/")[0])
        if policy is None:
            problems.append({"kind": "no_policy", "entry": entry})
            stats[entry] = {"n": 0, "dup": 0, "shadow": 0, "examples": []}
            continue

        st = {"n": 0, "dup": 0, "shadow": 0, "examples": []}
        skipped: dict[str, int] = {}                   # 不支持的类型 → 条数
        for line in fragment_rules(frag):
            f = [x.strip() for x in line.split(",")]
            t = f[0].upper()
            if t not in SUPPORTED_RULE_TYPES:
                # 上游里有 mihomo 不支持的类型（如 URL-REGEX）。文件保持纯镜像，
                # 这里忽略掉并报一行汇总——不静默，因为这种类型一旦写进配置
                # 就是整份加载失败（实测 -t 会 failed），得让人知道被跳过了。
                skipped[t] = skipped.get(t, 0) + 1
                continue
            st["n"] += 1
            v = f[1].lower() if len(f) > 1 else ""
            if (t, v) in seen_exact:                   # 同键的后续出现
                st["dup"] += 1
                continue
            if why := shadow_reason(t, v, seen_kw, seen_sfx):
                st["shadow"] += 1
                if len(st["examples"]) < 2:
                    st["examples"].append((line, why))
                if prune:                              # 只有剪枝模式才真的丢
                    continue
            kept.append((entry, policy, line))
            seen_exact.add((t, v))                     # 留下来的才能当遮蔽源
            if t == "DOMAIN-KEYWORD":
                seen_kw.add(v)
            elif t == "DOMAIN-SUFFIX":
                seen_sfx.add(v)
        stats[entry] = st
        if skipped:
            problems.append({"kind": "unsupported", "entry": entry, "types": skipped})

    # 磁盘上有、但顺序表里没列的片段会被静默忽略——这是个坑，必须提醒
    listed = {e for e, _ in order if not e.startswith("[]")}
    for f in sorted(RULES_DIR.rglob("*.list")):
        rel = str(f.relative_to(RULES_DIR))
        if rel not in listed:
            problems.append({"kind": "unlisted", "entry": rel})
    return kept, stats, problems


def cmd_rules_order(_: argparse.Namespace) -> int:
    order, origin = read_order()
    _, stats, problems = walk_order(order, prune=False)
    print(dim(f"片段顺序（{origin}）"))
    print()
    print(f"  {'#':>3}  {pad('片段', 34)}{'规则数':>8}{'同键重复':>9}{'被遮蔽':>8}  目标")
    total = t_dup = t_shadow = 0
    for i, (entry, policy) in enumerate(order, 1):
        if entry.startswith("[]"):
            print(f"  {i:>3}  {pad(dim('（内联规则）'), 34)}{'':>8}{'':>9}{'':>8}  {entry[2:]}")
            continue
        st = stats.get(entry, {"n": 0, "dup": 0, "shadow": 0, "examples": []})
        total += st["n"]
        t_dup += st["dup"]
        t_shadow += st["shadow"]
        pol = policy or POLICY_BY_DIR.get(entry.split("/")[0]) or warn("?")
        mark = "" if (RULES_DIR / entry).exists() else bad("  ← 缺失")
        dup = dim(f"{st['dup']:>9}") if st["dup"] else dim(f"{'-':>9}")
        sh = warn(f"{st['shadow']:>8}") if st["shadow"] else dim(f"{'-':>8}")
        print(f"  {i:>3}  {pad(entry, 34)}{st['n']:>8}{dup}{sh}  {pol}{mark}")
    print()
    print(f"  合计 {total} 条：{t_dup} 条同键重复、{t_shadow} 条被更宽的规则遮蔽")
    print(dim("  后两类先到先得，都不会生效；apply 默认去重，加 --prune 连遮蔽的一起去掉"))
    if problems:
        print()
        report_problems(problems)
    ex = [(e, st["examples"]) for e, st in stats.items() if st["examples"]]
    if ex:
        print()
        print(dim("  遮蔽样例："))
        for entry, examples in ex[:5]:
            for line, why in examples:
                print(f"    {entry}  {bad(line)} {dim('→ ' + why)}")
    return 0


def cmd_rules_diff(args: argparse.Namespace) -> int:
    rules, problems, origin, dedup = build_rules(prune=args.prune)
    n_inline = sum(1 for e, _ in read_order()[0] if e.startswith("[]"))
    head, cur, tail = split_config(require_config().read_text(encoding="utf-8"))

    # 注意：同一个 (类型,值) 可能出现在多个片段里。mihomo 先到先得，
    # 所以映射必须保留**第一次**出现的那条，用 setdefault 而不是字典推导（后者留最后一条）。
    cur_map: dict[tuple[str, str], str] = {}
    for l in cur:
        cur_map.setdefault(rule_key(l), l)
    new_map: dict[tuple[str, str], str] = {}
    for l in rules:
        new_map.setdefault(rule_key(l), l)
    added = [l for l in rules if rule_key(l) not in cur_map]
    removed = [l for l in cur if rule_key(l) not in new_map]
    changed = [k for k in cur_map.keys() & new_map.keys() if cur_map[k] != new_map[k]]

    print(dim(f"规则来源：{RULES_DIR}"))
    print(dim(f"片段顺序：{origin}"))
    print()
    print(f"  片段合计                      {dedup['raw']:>7} 条")
    if args.prune:
        print(f"  去重 + 剔除被遮蔽（--prune） {dim('-' + str(dedup['duplicates'] + dedup['shadowed'])):>8}")
    else:
        print(f"  去重（同类型+值只留第一条）   {dim('-' + str(dedup['duplicates'])):>8}")
        if dedup["shadowed"]:
            print(dim(f"  （另有 {dedup['shadowed']} 条被更宽的规则遮蔽，加 --prune 一并去掉）"))
    print(f"  应用后                        {len(rules):>7} 条" + dim(f"（含 {n_inline} 条内联规则）"))
    print()
    print(f"  现网 {config_path().name}              {len(cur):>7} 条")
    print()
    print(f"  {ok('新增')} {len(added):>7} 条")
    print(f"  {bad('删除')} {len(removed):>7} 条" + (dim("   ← 现网有、rules/ 树里没有") if removed else ""))
    print(f"  {warn('改策略')} {len(changed):>5} 条" + (dim("   ← 同域名不同目标，先出现的赢") if changed else ""))

    if removed:
        print()
        print(dim("  会被删掉的（前 10 条）："))
        for l in removed[:10]:
            print(f"    {bad('-')} {l}")
        if len(removed) > 10:
            print(dim(f"    …还有 {len(removed) - 10} 条"))
    if changed:
        print()
        print(dim("  改了策略的（前 10 条）："))
        for k in changed[:10]:
            print(f"    {warn('~')} {cur_map[k]}")
            print(f"      {dim('→')} {new_map[k]}")
    if problems:
        print()
        report_problems(problems)

    print()
    print(dim("这只是对比，没有写入任何文件。要落地就跑 mihomo-cli rules apply"))
    return 0


def cmd_rules_apply(args: argparse.Namespace) -> int:
    rules, problems, origin, dedup = build_rules(prune=args.prune)
    if not rules:
        die("拼出来 0 条规则，拒绝写入（检查 rules/order.txt 和片段是否为空）")

    cfg = require_config()
    text = cfg.read_text(encoding="utf-8")
    head, cur, tail = split_config(text)
    if problems:
        report_problems(problems)
        print()

    bak = backup_config()
    print(f"{ok('✓')} 已备份 {dim(str(bak))}")

    cfg.write_text(head + "".join(f"- {r}\n" for r in rules) + tail,
                   encoding="utf-8", newline="\n")
    note = f"（片段 {dedup['raw']} 条"
    if args.prune:
        note += f"，去重+剔除被遮蔽 {dedup['duplicates'] + dedup['shadowed']} 条"
    else:
        note += f"，去重 {dedup['duplicates']} 条"
    note += f"；原配置 {len(cur)} 条，{cfg.stat().st_size / 1024 / 1024:.2f} MB）"
    print(f"{ok('✓')} 已写入 {len(rules)} 条规则" + dim(note))

    good, last = validate_config()
    if not good:
        shutil.copy2(bak, cfg)                      # 回滚
        print(bad(f"✗ mihomo -t 校验失败，已回滚到 {bak}"))
        print(bad(f"  {last}"))
        return 1
    print(f"{ok('✓')} mihomo -t 校验通过  {dim(last)}")

    if args.reload:
        if reload_config():
            print(f"{ok('✓')} 已热重载运行中的 mihomo  {dim('（通过 external-controller API）')}")
        else:
            print(warn(f"⚠ 热重载失败，配置文件已写入，可以 {RESTART_HINT}"))
    else:
        print(dim("  没有热重载；加 --reload 让它立即生效（否则等下次重启 mihomo）"))
    return 0


