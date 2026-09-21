"""规则树：把家目录里的 ACL4SSR 片段拼成 config.yaml 的 rules: 区块。

片段在 ~/.config/mihomo-cli/rules/，顺序表是下面的 CANONICAL_ORDER。
动作：sync 同步片段 / diff 对比现网 / apply 落地（备份→校验→失败回滚）/ rollback 回滚。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .core import (
    BACKUP_DIR,
    RESTART_HINT,
    TOOL_DIR,
    backup_config,
    bad,
    config_path,
    die,
    dim,
    fmt_ts,
    list_backups,
    ok,
    reload_config,
    require_config,
    run,
    size_str,
    validate_config,
    warn,
)

# ─────────────────────── rules 子命令 ───────────────────────
# 把家目录里的片段拼成 mihomo 的 rules: 区块：片段不带策略（策略由目录名决定），
# 顺序由下面的 CANONICAL_ORDER 决定。

RULES_DIR = TOOL_DIR / "rules"  # 家目录里那份，不是仓库里那份

# 目录名 → 策略。目录名就是"这批规则要往哪走"。
# 注意这些名字必须能在 config.yaml 里找到（组名或内建策略），mihomo 会校验。
POLICY_BY_DIR = {"proxy": "节点选择", "direct": "全球直连", "reject": "全球拦截"}

# 片段里出现在 payload 之后的字段是**参数**而不是策略，
# 拼策略时必须插在它们前面：IP-CIDR,1.2.3.0/24,no-resolve → ...,DIRECT,no-resolve
RULE_PARAMS = {"no-resolve", "src", "dport"}

# mihomo 1.19 支持的类型；不在表里的会跳过并告警——写进配置会让**整份**加载失败。
SUPPORTED_RULE_TYPES = {
    "DOMAIN",
    "DOMAIN-SUFFIX",
    "DOMAIN-KEYWORD",
    "DOMAIN-REGEX",
    "IP-CIDR",
    "IP-CIDR6",
    "IP-SUFFIX",
    "IP-ASN",
    "SRC-IP-CIDR",
    "GEOIP",
    "GEOSITE",
    "PROCESS-NAME",
    "PROCESS-PATH",
    "DST-PORT",
    "SRC-PORT",
    "NETWORK",
    "RULE-SET",
    "MATCH",
    "FINAL",
}

# 片段拼接顺序（**行序就是优先级**）：先到先得，同一域名被多个片段命中时排上面的赢。
#
# 分层：局域网 → 误拦白名单 → 拦截 → 我自己的 → 必须直连的服务 → 必须代理的服务
#       → 地域/墙的大清单（越具体越靠前）→ 兜底。依据：越硬的意图越靠前；清单越具体
#       越靠前（冲突时它的意图更明确），越糊的越靠后。
#
# 硬约束：① LocalAreaNetwork 必须第 1，否则 .local 这类保留域会被广告规则抢走；
#         ② GoogleFCM / GoogleCN 必须排在所有代理片段之前，否则会被 proxy 里的
#            DOMAIN-KEYWORD,google 整片吃掉（不报错，只是静默走错）。
#
# 改这里就是改优先级（删一行 = 不应用那个片段），然后 rules diff / apply。
# 目录名只决定策略（proxy→节点选择 / direct→全球直连 / reject→全球拦截）。
# Custom.list 是你自己的片段（sync 时缺了会建空的），[] 开头的是内联规则。
CANONICAL_ORDER: list[tuple[str, str | None]] = [
    # 层 0：局域网/保留域 —— 必须最先
    ("direct/LocalAreaNetwork.list", "全球直连"),
    # 层 2：拦截（自己的放最前，语义清楚；目标都是全球拦截，内部顺序无副作用）
    ("reject/Custom.list", "全球拦截"),
    ("reject/BanAD.list", "全球拦截"),
    ("reject/BanProgramAD.list", "全球拦截"),
    ("reject/BanEasyList.list", "全球拦截"),
    ("reject/BanEasyListChina.list", "全球拦截"),
    ("reject/BanEasyPrivacy.list", "全球拦截"),
    # 层 3：我自己的规则 —— 优先于上游模板
    ("direct/Custom.list", "全球直连"),
    ("proxy/Custom.list", "节点选择"),
    # 层 4：必须直连的服务（小、精确；走代理会功能异常）
    # 注：GoogleFCM.list 上游头部写「数量：35条」，实际 44 条（18 DOMAIN + 26 IP-CIDR）
    ("direct/GoogleFCM.list", "全球直连"),
    ("direct/GoogleCN.list", "全球直连"),
    ("direct/Apple.list", "全球直连"),
    # 层 5：必须代理的服务（小、精确；走代理是预期行为）
    # 注：Telegram.list 13 条
    ("proxy/Telegram.list", "节点选择"),
    # 层 6：地域/墙的大清单，从「较具体」到「最糊」
    ("direct/ChinaMedia.list", "全球直连"),  # 国内媒体：比较具体
    ("proxy/ProxyLite.list", "节点选择"),  # 精选墙名单（430 条）
    ("proxy/ProxyMedia.list", "节点选择"),  # 国外媒体
    ("proxy/ProxyGFWlist.list", "节点选择"),  # GFW 全量（6986 条）
    ("direct/ChinaDomain.list", "全球直连"),  # 整个 .cn —— 最糊的域名兜底
    ("direct/ChinaCompanyIp.list", "全球直连"),
    # 层 7：按 IP 判断的兜底，放在域名规则之后（IP 是另一个维度）
    ("direct/ChinaIp.list", "全球直连"),
    ("direct/ChinaIpV6.list", "全球直连"),
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


def with_policy(line: str, policy: str | None) -> str:
    """给片段里的规则补上策略。"""
    f = [x.strip() for x in line.split(",")]
    if f[0].upper() in ("MATCH", "FINAL"):  # 没有 payload
        return ",".join(f[:1] + ([policy] if policy else []) + f[1:])
    payload, rest = f[1] if len(f) > 1 else "", f[2:]
    if rest and rest[0] not in RULE_PARAMS:
        return ",".join(f)
    return ",".join([f[0], payload] + ([policy] if policy else []) + rest)


def build_rules(prune: bool = False) -> tuple[list[str], list[dict], str, dict]:
    """按 CANONICAL_ORDER 拼出完整的规则行。返回 (规则, 问题, 顺序来源, 统计)。"""
    origin = f"内置顺序（rules.py 的 CANONICAL_ORDER，{len(CANONICAL_ORDER)} 条）"
    kept, stats, problems = walk_order(CANONICAL_ORDER, prune)

    rules = [
        line if entry.startswith("[]") else with_policy(line, policy)
        for entry, policy, line in kept
    ]
    total = {
        "n": sum(s["n"] for s in stats.values()),
        "dup": sum(s["dup"] for s in stats.values()),
        "shadow": sum(s["shadow"] for s in stats.values()),
    }
    return (
        rules,
        problems,
        origin,
        {
            "raw": total["n"],
            "duplicates": total["dup"],
            "shadowed": total["shadow"],
            "stats": stats,
        },
    )


def split_config(text: str) -> tuple[str, list[str], str]:
    """把 config.yaml 拆成 (rules: 之前, 现有规则, rules: 之后)。"""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.startswith("rules:")), None)
    if start is None:
        die(f"{config_path()} 里找不到 rules: 区块")
    end = start + 1
    while end < len(lines) and (lines[end].startswith("- ") or not lines[end].strip()):
        end += 1
    head, tail = "".join(lines[: start + 1]), "".join(lines[end:])
    cur = [line[2:].strip() for line in lines[start + 1 : end] if line.startswith("- ")]
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
            print(
                warn(
                    f"  ⚠ {p['entry']} 跳过 {sum(p['types'].values())} 条 "
                    f"mihomo 不支持的类型：{detail}"
                )
            )


def cmd_rules_rollback(args: argparse.Namespace) -> int:
    cfg = require_config()
    items = list_backups()
    if not items:
        die(f"没有可用备份。找过这两个地方：\n    {BACKUP_DIR}\n    {cfg.parent}")

    print(dim("可用备份（新 → 旧）："))
    for i, (ts, p, src) in enumerate(items, 1):
        where = "工具目录" if src == BACKUP_DIR else "config 同级"
        mark = ok("← 默认") if i == 1 else ""
        print(f"  {i:>2}  {fmt_ts(ts):<28}  {size_str(p.stat().st_size):>9}  {dim(where)}  {mark}")
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
        shutil.copy2(keep, cfg)  # 回滚的回滚
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


# 片段来源：本地 ACL4SSR clone（--from 指定，无默认值；只读工作区，不联网、不替你 clone）。
# 注：GoogleCN/Apple/Telegram/ProxyGFWlist 在 ACL4SSR 里顶层和 Ruleset/ 下都有，用顶层那份。

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


def ensure_custom_fragments() -> list[str]:
    """把顺序表里提到的 Custom.list 补成空文件，返回补了哪些。

    拉完就是一个能直接 apply 的自洽树：想加规则随时往里写。已有文件绝不碰。"""
    added = []
    for entry, _ in CANONICAL_ORDER:
        if entry.startswith("[]") or Path(entry).name != "Custom.list":
            continue
        dst = RULES_DIR / entry
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("", encoding="utf-8")
        added.append(entry)
    return added


def local_clone_root(given: str) -> tuple[Path, str]:
    """把 --from 给的目录归一成「clone 根目录」，返回 (根, 给人看的说明)。

    人很容易把哪个目录当成"规则目录"。给错了就报清楚，别等 18 个文件全失败。"""
    p = Path(given).expanduser()
    if not p.is_dir():
        die(f"--from 给的这个目录不存在（或不是目录）：{p}")
    if (p / "Clash").is_dir():
        root = p
    elif p.name == "Clash" and any(p.glob("*.list")):
        root = p.parent  # 直接给了 Clash/，往下拼时要去掉这层
    else:
        die(
            f"{p} 看着不是 ACL4SSR 的 clone（里面没有 Clash/ 目录）。\n"
            f"  给 clone 的根目录，例如：--from ~/GitHub/ACL4SSR\n"
            f"  （bare clone 没有工作区文件，得给普通 clone 的路径）"
        )
    probe = root / "Clash/LocalAreaNetwork.list"
    if not probe.is_file():
        die(f"{root} 里找不到 Clash/LocalAreaNetwork.list，不像是完整的 ACL4SSR clone。")

    # 顺手把 clone 停在哪个 commit 报出来：用这个模式多半就是为了锁版本
    info = ""
    if shutil.which("git"):
        g = run("git", "-C", str(root), "log", "-1", "--format=%h %ad", "--date=short")
        if g.returncode == 0 and g.stdout.strip():
            info = f"（clone 停在 {g.stdout.strip()}）"
    return root, f"本地 clone {root}{info}"


def cmd_rules_sync(args: argparse.Namespace) -> int:
    """从本地 ACL4SSR clone 同步那 18 个片段（--from 指位置）。**不联网**。

    不做网络拉取：把网络依赖塞进「改规则」这条路，被墙/超时都会让人卡住；clone 你自己 pull。"""
    root, label = local_clone_root(args.from_dir)
    print(dim(f"片段来源：{label}"))
    print()

    def grab(rel: str) -> bytes:
        src = root / UPSTREAM[rel]
        if not src.is_file():
            raise FileNotFoundError(f"clone 里没有 {UPSTREAM[rel]}")
        return src.read_bytes()

    added = updated = same = failed = 0
    for rel in sorted(UPSTREAM):
        dst = RULES_DIR / rel
        try:
            data = grab(rel)
        except OSError as e:
            print(bad(f"  ✗ {rel}  同步失败：{e}"))
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
        dst.write_bytes(data)  # 上游原文，一字不改
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

    if not args.dry_run:
        # 顺序表里还引用了你自己的 Custom.list，缺了就建个空的（理由见那个函数）
        stubs = ensure_custom_fragments()
        if stubs:
            print(f"{ok('✓')} 已建空的自定义片段 " + "、".join(dim(x) for x in stubs))
            print(dim("    这些是你自己的片段（上游不会给），想加规则就往里写"))

    if args.dry_run:
        print(dim("  --dry-run：什么都没写。去掉它才会真的同步进家目录。"))
    else:
        print(dim("  这些是 clone 工作区里的上游原文；接着跑 mihomo-cli rules diff 看差异"))
    return 1 if failed else 0


def cmd_rules(args: argparse.Namespace) -> int:
    action = getattr(args, "rules_action", None) or "diff"  # 不带则默认 diff，只读
    # fetch 是 sync 的老名字（argparse 存的是命令行上写的那个词，跟顶层的
    # services/list/ls 一样，得自己映射回正名）
    action = {"fetch": "sync"}.get(action, action)
    if action != "sync" and not RULES_DIR.is_dir():
        # 片段现在住家目录（以前在仓库里），新机器/新用户第一次跑必然碰不到，
        # 与其抛一堆“文件不存在”警告，不如直接把该跑的命令给出来
        print(warn(f"⚠ 还没拉过规则片段（{RULES_DIR} 不存在）"), file=sys.stderr)
        print(dim("  先跑：mihomo-cli rules sync"), file=sys.stderr)
    if not hasattr(args, "prune"):
        args.prune = False  # 没走子解析器时没有这个属性
    return {
        "diff": cmd_rules_diff,
        "sync": cmd_rules_sync,
        "apply": cmd_rules_apply,
        "rollback": cmd_rules_rollback,
    }[action](args)


def shadow_reason(t: str, v: str, seen_kw: set[str], seen_sfx: set[str]) -> str | None:
    """这条规则会不会被前面某条更宽的规则吃掉？返回原因，否则 None。

    每条判据都必须保证“更早那条能匹配本条能匹配的一切”——否则就会把活规则"""
    if not v:
        return None
    parents = {".".join(v.split(".")[k:]) for k in range(len(v.split(".")))}
    if t == "DOMAIN":
        # 同值的 DOMAIN-SUFFIX 也能拿它，所以要带上 v 自己
        if hit := parents & seen_sfx:
            return f"被 DOMAIN-SUFFIX,{sorted(hit)[0]} 覆盖"
    elif t == "DOMAIN-SUFFIX":
        if hit := (parents - {v}) & seen_sfx:  # 排除自己那一层
            return f"被 DOMAIN-SUFFIX,{sorted(hit)[0]} 覆盖"
    elif t != "DOMAIN-KEYWORD":
        return None
    if t in ("DOMAIN", "DOMAIN-SUFFIX"):
        if k := next((k for k in seen_kw if k in v), None):
            return f"被 DOMAIN-KEYWORD,{k} 覆盖"
    else:  # DOMAIN-KEYWORD
        if k := next((k for k in seen_kw if k != v and k in v), None):
            return f"被 DOMAIN-KEYWORD,{k} 覆盖"
    return None


def walk_order(
    order: list[tuple[str, str | None]], prune: bool
) -> tuple[list[tuple[str, str | None, str]], dict[str, dict], list[dict]]:
    """按顺序扫一遍，算出哪些规则真正生效。"""
    seen_exact: set[tuple[str, str]] = set()
    seen_kw: set[str] = set()
    seen_sfx: set[str] = set()
    kept: list[tuple[str, str | None, str]] = []
    stats: dict[str, dict] = {}
    problems: list[dict] = []

    for entry, explicit in order:
        if entry.startswith("[]"):  # 内联规则，自带策略
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
        skipped: dict[str, int] = {}  # 不支持的类型 → 条数
        for line in fragment_rules(frag):
            f = [x.strip() for x in line.split(",")]
            t = f[0].upper()
            if t not in SUPPORTED_RULE_TYPES:
                # mihomo 不支持的类型（如 URL-REGEX）：文件保持上游原文，构建时跳过并汇总告警。
                skipped[t] = skipped.get(t, 0) + 1
                continue
            st["n"] += 1
            v = f[1].lower() if len(f) > 1 else ""
            if (t, v) in seen_exact:  # 同键的后续出现
                st["dup"] += 1
                continue
            if why := shadow_reason(t, v, seen_kw, seen_sfx):
                st["shadow"] += 1
                if len(st["examples"]) < 2:
                    st["examples"].append((line, why))
                if prune:  # 只有剪枝模式才真的丢
                    continue
            kept.append((entry, policy, line))
            seen_exact.add((t, v))  # 留下来的才能当遮蔽源
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


def cmd_rules_diff(args: argparse.Namespace) -> int:
    rules, problems, origin, dedup = build_rules(prune=args.prune)
    n_inline = sum(1 for e, _ in CANONICAL_ORDER if e.startswith("[]"))
    _head, cur, _tail = split_config(require_config().read_text(encoding="utf-8"))

    # 注意：同一个 (类型,值) 可能出现在多个片段里。mihomo 先到先得，
    # 所以映射必须保留**第一次**出现的那条，用 setdefault 而不是字典推导（后者留最后一条）。
    cur_map: dict[tuple[str, str], str] = {}
    for line in cur:
        cur_map.setdefault(rule_key(line), line)
    new_map: dict[tuple[str, str], str] = {}
    for line in rules:
        new_map.setdefault(rule_key(line), line)
    added = [line for line in rules if rule_key(line) not in cur_map]
    removed = [line for line in cur if rule_key(line) not in new_map]
    changed = [k for k in cur_map.keys() & new_map.keys() if cur_map[k] != new_map[k]]

    print(dim(f"规则来源：{RULES_DIR}"))
    print(dim(f"片段顺序：{origin}"))
    print()
    print(f"  片段合计                      {dedup['raw']:>7} 条")
    if args.prune:
        print(
            f"  去重 + 剔除被遮蔽（--prune） {dim('-' + str(dedup['duplicates'] + dedup['shadowed'])):>8}"
        )
    else:
        print(f"  去重（同类型+值只留第一条）   {dim('-' + str(dedup['duplicates'])):>8}")
        if dedup["shadowed"]:
            print(dim(f"  （另有 {dedup['shadowed']} 条被更宽的规则遮蔽，加 --prune 一并去掉）"))
    print(
        f"  应用后                        {len(rules):>7} 条" + dim(f"（含 {n_inline} 条内联规则）")
    )
    print()
    print(f"  现网 {config_path().name}              {len(cur):>7} 条")
    print()
    print(f"  {ok('新增')} {len(added):>7} 条")
    print(
        f"  {bad('删除')} {len(removed):>7} 条"
        + (dim("   ← 现网有、片段里没有") if removed else "")
    )
    print(
        f"  {warn('改策略')} {len(changed):>5} 条"
        + (dim("   ← 同域名不同目标，先出现的赢") if changed else "")
    )

    if removed:
        print()
        print(dim("  会被删掉的（前 10 条）："))
        for line in removed[:10]:
            print(f"    {bad('-')} {line}")
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
    rules, problems, _origin, dedup = build_rules(prune=args.prune)
    if not rules:
        die("拼出来 0 条规则，拒绝写入（片段是不是都没同步过来？先 rules sync）")

    # 片段缺失必须拒绝写入：apply 是整块替换 rules: 区块，少一个片段就等于把那片规则删掉。
    missing = [p["entry"] for p in problems if p["kind"] == "missing"]
    if missing:
        shown = "\n".join(f"    {e}" for e in missing[:8])
        more = f"\n    …还有 {len(missing) - 8} 个" if len(missing) > 8 else ""
        custom = [e for e in missing if Path(e).name == "Custom.list"]
        # Custom.list 是用户自己的片段，上游不会给——这时候说“去 sync”是废话，
        # 得直接告诉他文件长什么样、怎么补个空的
        hint = (
            (
                f"\n  其中 {len(custom)} 个是 Custom.list——你自己的片段，上游不会给：\n"
                f"  从备份恢复 {RULES_DIR}，或者建个空的（空 = 没有自定义规则）：\n"
                + "\n".join(f"    : > {RULES_DIR / e}" for e in custom[:3])
            )
            if custom
            else ""
        )
        sync_line = (
            "" if len(custom) == len(missing) else "  要么先把片段同步过来：mihomo-cli rules sync\n"
        )
        edit_line = "把该片段从 rules.py 的 CANONICAL_ORDER 里去掉（顺序表在代码里）"
        die(
            f"有 {len(missing)} 个片段文件不存在，拒绝写入。\n{shown}{more}{hint}\n"
            f"  照现在这样写下去，这些片段管的规则会被整片删掉。\n"
            f"{sync_line}"
            f"  要么确实不用它们了：{edit_line}"
        )

    cfg = require_config()
    text = cfg.read_text(encoding="utf-8")
    head, cur, tail = split_config(text)
    if problems:
        report_problems(problems)
        print()

    bak = backup_config()
    print(f"{ok('✓')} 已备份 {dim(str(bak))}")

    cfg.write_text(head + "".join(f"- {r}\n" for r in rules) + tail, encoding="utf-8", newline="\n")
    note = f"（片段 {dedup['raw']} 条"
    if args.prune:
        note += f"，去重+剔除被遮蔽 {dedup['duplicates'] + dedup['shadowed']} 条"
    else:
        note += f"，去重 {dedup['duplicates']} 条"
    note += f"；原配置 {len(cur)} 条，{cfg.stat().st_size / 1024 / 1024:.2f} MB）"
    print(f"{ok('✓')} 已写入 {len(rules)} 条规则" + dim(note))

    good, last = validate_config()
    if not good:
        shutil.copy2(bak, cfg)  # 回滚
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
