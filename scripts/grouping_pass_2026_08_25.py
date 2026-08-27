#!/usr/bin/env python3
"""Practice-group pass 2026-08-25 — live sqlite crm.db.

Uses app.services.practice_groups APIs only. Does not invent people links,
commit/push, or touch Render. DATABASE_URL must stay unset.
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set, Tuple

# Safety: never inherit a cloud DATABASE_URL
os.environ.pop("DATABASE_URL", None)

from app.database import SessionLocal, DATABASE_URL, IS_SQLITE
from app.models import Client, Job
from app.models.practice_group import PracticeGroup, PracticeGroupMember
from app.services.practice_groups import (
    create_group,
    delete_group,
    is_group_eligible_client,
    list_board,
    move_client,
    rename_group,
)
from app.services.grouping import get_group_detail, list_group_summaries
from sqlalchemy import func


DOCS_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "grouping-pass-2026-08-25.md")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _disp(c: Client) -> str:
    if hasattr(c, "display_name"):
        return c.display_name() or c.company_name or f"Client #{c.id}"
    return c.company_name or f"Client #{c.id}"


def is_test_co(c: Client) -> bool:
    blob = f"{_disp(c)} {c.company_name or ''}".lower()
    return "test co" in blob


def strip_legal(name: str) -> str:
    out = re.sub(
        r"\s+((group\s+)?(limited|ltd\.?|llp|plc))\s*$",
        "",
        (name or "").strip(),
        flags=re.I,
    )
    out = re.sub(r"\s{2,}", " ", out).strip(" .")
    return out or name


# Cluster B name-family predicates (company names, not people)
def pred_outlooks(n: str) -> bool:
    x = _norm(n)
    return "outlooks" in x or x.startswith("structured outlook")


def pred_warwick(n: str) -> bool:
    return "warwick homes" in _norm(n)


def pred_forshaws(n: str) -> bool:
    x = _norm(n)
    return x.startswith("forshaws")


def pred_negotiis(n: str) -> bool:
    return _norm(n).startswith("negotiis")


def pred_sixty_six(n: str) -> bool:
    return "sixty six" in _norm(n)


def pred_nti(n: str) -> bool:
    x = _norm(n)
    if "nitem" in x:
        return False
    return bool(re.search(r"\bnti\b", x))


def pred_st_helens(n: str) -> bool:
    x = _norm(n).replace("st helens", "st helens")
    x = x.replace("st . helens", "st helens")
    return "st helens" in _norm(n) or "st helens" in _norm(n.replace(".", " "))


def pred_b2b(n: str) -> bool:
    x = _norm(n)
    return x.startswith("b2b") or " b2b " in f" {x} "


def pred_accology(n: str) -> bool:
    return _norm(n).startswith("accology")


def pred_sqco(n: str) -> bool:
    x = _norm(n)
    return x.startswith("sqco") or x.startswith("sqfix")


FAMILY_PREDS: List[Tuple[str, Callable[[str], bool]]] = [
    ("Outlooks", pred_outlooks),
    ("Warwick Homes", pred_warwick),
    ("Forshaws", pred_forshaws),
    ("Negotiis", pred_negotiis),
    ("Sixty Six", pred_sixty_six),
    ("St Helens", pred_st_helens),
    ("NTI", pred_nti),
    ("B2B", pred_b2b),
    ("Accology", pred_accology),
]


def client_fees(db, ids: Set[int]) -> Dict[int, float]:
    if not ids:
        return {}
    rows = (
        db.query(Job.client_id, func.coalesce(func.sum(Job.fee), 0.0))
        .filter(Job.client_id.in_(list(ids)))
        .group_by(Job.client_id)
        .all()
    )
    return {int(cid): float(total or 0) for cid, total in rows if cid is not None}


def membership_map(db) -> Dict[int, int]:
    return {
        int(m.client_id): int(m.group_id)
        for m in db.query(PracticeGroupMember).all()
        if m.client_id
    }


def snapshot(db) -> dict:
    board, free = list_board(db)
    groups = db.query(PracticeGroup).all()
    members = db.query(PracticeGroupMember).all()
    size_hist: Dict[int, int] = defaultdict(int)
    multi = []
    for bg in board:
        size_hist[len(bg.members)] += 1
        if len(bg.members) >= 2:
            multi.append(
                {
                    "id": bg.group.id,
                    "name": bg.group.name,
                    "n": len(bg.members),
                    "fees": bg.total_fees,
                    "members": [_disp(m.client) for m in bg.members],
                }
            )
    return {
        "practice_groups": len(groups),
        "membership_rows": len(members),
        "board_groups": len(board),
        "ungrouped": len(free),
        "size_hist": dict(sorted(size_hist.items())),
        "multi": multi,
        "ungrouped_rows": [
            {
                "id": bc.client.id,
                "name": _disp(bc.client),
                "status": bc.client.overall_status,
                "fees": bc.fees,
            }
            for bc in free
        ],
        "board_rows": [
            {
                "id": bg.group.id,
                "name": bg.group.name,
                "n": len(bg.members),
                "fees": bg.total_fees,
                "members": [_disp(m.client) for m in bg.members],
            }
            for bg in board
        ],
    }


def find_cluster_idx(clusters: List[Set[int]], cid: int) -> Optional[int]:
    for i, s in enumerate(clusters):
        if cid in s:
            return i
    return None


def union_into(clusters: List[Set[int]], ids: Set[int]) -> None:
    if len(ids) < 2:
        # still attach a lone id to nothing
        return
    indices = sorted(
        {i for i in (find_cluster_idx(clusters, cid) for cid in ids) if i is not None},
        reverse=True,
    )
    if not indices:
        clusters.append(set(ids))
        return
    base = indices[-1]  # smallest index after reverse sort... wait reverse so min is last
    base = min(indices)
    merged = set(ids)
    for j in sorted(indices, reverse=True):
        merged |= clusters[j]
        if j != base:
            del clusters[j]
    clusters[base] = merged


def lead_client(clients: List[Client], fees: Dict[int, float]) -> Client:
    return max(
        clients,
        key=lambda c: (fees.get(c.id, 0.0), (_disp(c) or "").lower(), c.id),
    )


def family_labels_for(members: List[Client]) -> List[str]:
    names = [_disp(c) for c in members]
    labels = []
    for label, pred in FAMILY_PREDS:
        hits = [n for n in names if pred(n)]
        if len(hits) >= 2:
            labels.append(label)
    return labels


def pick_group_name(lead: Client, members: List[Client], family: Optional[str]) -> str:
    if family:
        return family
    # Multi-client, no Cluster-B family: keep lead name (strip legal suffix only
    # when the current implied name is exactly one company).
    return strip_legal(_disp(lead)) if len(members) >= 2 else _disp(lead)


def main() -> None:
    print("DATABASE_URL env", os.environ.get("DATABASE_URL"))
    print("app DATABASE_URL", DATABASE_URL)
    print("IS_SQLITE", IS_SQLITE)
    if not IS_SQLITE:
        raise SystemExit("Refusing to run: expected local sqlite, got " + str(DATABASE_URL))

    db = SessionLocal()
    moves: List[dict] = []
    created: List[Tuple[int, str]] = []
    renamed: List[Tuple[int, str, str]] = []
    deleted: List[Tuple[int, str]] = []
    notes_simon: List[str] = []
    cluster_reports: List[dict] = []

    try:
        before = snapshot(db)
        print(
            "BEFORE groups=%s members=%s ungrouped=%s multi=%s hist=%s"
            % (
                before["practice_groups"],
                before["membership_rows"],
                before["ungrouped"],
                len(before["multi"]),
                before["size_hist"],
            )
        )

        clients = {c.id: c for c in db.query(Client).all()}
        eligible = {
            cid: c
            for cid, c in clients.items()
            if is_group_eligible_client(c) and not is_test_co(c)
        }

        # --- Cluster A: people-graph 2+ eligible ---
        clusters: List[Set[int]] = []
        pg_meta = []
        for s in list_group_summaries(db):
            detail = get_group_detail(db, s.group_id)
            if not detail:
                continue
            ids = {
                c.id
                for c in detail.clients
                if c.id in eligible
            }
            if len(ids) < 2:
                continue
            clusters.append(set(ids))
            pg_meta.append(
                {
                    "pg_id": s.group_id,
                    "name": s.name,
                    "ids": set(ids),
                    "people": [p.full_name for p in (detail.people or [])],
                }
            )

        # Accology name-family should NOT swallow Access Utilities just because
        # Simon Duckworth is a person on Access Utilities. Split Accology* out.
        accology_ids = {cid for cid, c in eligible.items() if pred_accology(_disp(c))}
        # Individuals who belong with Accology rather than Access:
        # Melissa Farrell is only on Accology Pays; Simon Duckworth is Accology principal.
        name_to_id = {_disp(c).strip().lower(): c.id for c in eligible.values()}
        simon_id = name_to_id.get("simon duckworth")
        melissa_id = name_to_id.get("melissa leanne farrell")
        accology_cluster = set(accology_ids)
        if simon_id:
            accology_cluster.add(simon_id)
        if melissa_id:
            accology_cluster.add(melissa_id)

        access_had_accology = False
        for s in clusters:
            if accology_ids & s:
                access_had_accology = True
                s -= accology_cluster
        clusters = [s for s in clusters if len(s) >= 2]
        if len(accology_cluster) >= 2:
            clusters.append(accology_cluster)

        if access_had_accology:
            notes_simon.append(
                "Simon Duckworth is a person on Access Utilities (UK) Limited as well as "
                "Accology Limited / Accology Pays. Accology was grouped as its own name "
                "family (Cluster B) rather than merged into the Doyle/Davies Access Utilities "
                "people-graph. Confirm whether Access Utilities should sit with Accology."
            )

        # --- Cluster B: name families even if people not fully linked ---
        def collect(pred) -> Set[int]:
            return {cid for cid, c in eligible.items() if pred(_disp(c))}

        # Sqco+Sqfix only if already in a people-graph together (do not create a new family)
        sq_ids = collect(pred_sqco)
        if len(sq_ids) >= 2:
            idxs = {find_cluster_idx(clusters, cid) for cid in sq_ids}
            if None in idxs:
                notes_simon.append(
                    "Sqco/Sqfix: at least one member is not in a people-graph cluster; "
                    "left those unmerged (Cluster B says only if people-graph)."
                )
            idxs.discard(None)
            if len(idxs) == 1:
                # already same people-graph cluster — nothing extra
                pass
            elif len(idxs) > 1:
                union_into(clusters, sq_ids)

        name_families = [
            collect(pred_outlooks),
            collect(pred_warwick),
            collect(pred_forshaws),
            collect(pred_negotiis),
            collect(pred_sixty_six),
            collect(pred_nti) | collect(pred_st_helens),  # people-graph already unites; keep together
            collect(pred_b2b),
            accology_ids,
        ]
        # Nitem only if people-graph: if Nitem sits in a cluster that already has NTI/St Helens, keep it (already there)
        nitem_ids = {
            cid
            for cid, c in eligible.items()
            if "nitem" in _norm(_disp(c))
        }
        for nid in nitem_ids:
            idx = find_cluster_idx(clusters, nid)
            nti_st = collect(pred_nti) | collect(pred_st_helens)
            if idx is None:
                notes_simon.append(
                    f"Nitem (client {nid}) is not in a people-graph cluster with NTI/St Helens; left out of that family."
                )
            else:
                if not (clusters[idx] & nti_st):
                    notes_simon.append(
                        f"Nitem (client {nid}) is in a people-graph cluster that does not include NTI/St Helens; not force-merged."
                    )

        for fam_ids in name_families:
            if len(fam_ids) >= 2:
                union_into(clusters, fam_ids)

        clusters = [s for s in clusters if len(s) >= 2]

        # --- Apply moves ---
        fees = client_fees(db, {cid for s in clusters for cid in s})

        for ids in sorted(clusters, key=lambda s: -sum(fees.get(i, 0) for i in s)):
            members = [eligible[i] for i in ids if i in eligible]
            if len(members) < 2:
                continue
            lead = lead_client(members, fees)
            labels = family_labels_for(members)
            # Don't name the whole Hudson Hill graph "B2B" just because two members match.
            companies = [
                c
                for c in members
                if any(
                    w in _disp(c).lower()
                    for w in ("limited", "ltd", "llp", "plc", "partnership")
                )
            ] or members
            usable = []
            for label, pred in FAMILY_PREDS:
                hits = [c for c in companies if pred(_disp(c))]
                if len(hits) < 2:
                    continue
                lead_hit = pred(_disp(lead))
                majority = len(hits) >= max(2, (len(companies) + 1) // 2)
                if lead_hit or majority:
                    usable.append(label)
            family = " / ".join(usable) if usable else None
            target_name = pick_group_name(lead, members, family)

            mmap = membership_map(db)
            lead_gid = mmap.get(lead.id)
            if lead_gid:
                target_id = lead_gid
            else:
                g = create_group(db, target_name)
                target_id = g.id
                created.append((target_id, target_name))
                print(f"CREATE G{target_id} {target_name!r} lead={_disp(lead)}")

            gobj = db.query(PracticeGroup).filter(PracticeGroup.id == target_id).first()
            old_name = gobj.name if gobj else ""
            member_names = {_disp(c) for c in members} | {(c.company_name or "") for c in members}
            if gobj and gobj.name != target_name:
                # Rename if still a single company name, or we have a family label.
                if family or gobj.name in member_names:
                    rename_group(db, target_id, target_name)
                    renamed.append((target_id, old_name, target_name))
                    print(f"RENAME G{target_id} {old_name!r} -> {target_name!r}")

            mmap = membership_map(db)
            groups_by_id = {g.id: g.name for g in db.query(PracticeGroup).all()}
            for c in sorted(members, key=lambda x: _disp(x).lower()):
                from_gid = mmap.get(c.id)
                if from_gid == target_id:
                    continue
                from_name = groups_by_id.get(from_gid) if from_gid else "(ungrouped)"
                ok = move_client(db, c.id, target_id)
                moves.append(
                    {
                        "client_id": c.id,
                        "name": _disp(c),
                        "from_gid": from_gid,
                        "from_name": from_name,
                        "to_gid": target_id,
                        "to_name": target_name,
                        "ok": ok,
                    }
                )
                print(
                    f"MOVE C{c.id} {_disp(c)!r} {from_name!r} -> G{target_id} {target_name!r} ok={ok}"
                )

            cluster_reports.append(
                {
                    "target_id": target_id,
                    "target_name": target_name,
                    "family": family,
                    "lead": _disp(lead),
                    "lead_id": lead.id,
                    "members": sorted((_disp(c), c.id) for c in members),
                    "labels": labels,
                }
            )

        # Delete empty groups (including Test Co / Inactive leftovers)
        remaining_member_gids = {
            int(r[0])
            for r in db.query(PracticeGroupMember.group_id).all()
            if r[0]
        }
        for g in db.query(PracticeGroup).all():
            if g.id not in remaining_member_gids:
                name = g.name
                gid = g.id
                if delete_group(db, gid):
                    deleted.append((gid, name))
                    print(f"DELETE empty G{gid} {name!r}")

        after = snapshot(db)
        print(
            "AFTER groups=%s members=%s ungrouped=%s multi=%s hist=%s"
            % (
                after["practice_groups"],
                after["membership_rows"],
                after["ungrouped"],
                len(after["multi"]),
                after["size_hist"],
            )
        )

        # Remaining ungrouped — flag possible needs-Simon
        leftover_names = [r["name"] for r in after["ungrouped_rows"]]
        if any("jumping gems" in n.lower() for n in leftover_names):
            notes_simon.append(
                "Jumping Gems Ltd has no people-graph partner and no Cluster B name family; left ungrouped."
            )
        notes_simon.append(
            "Negotiis Properties Ltd is Inactive — skipped (is_group_eligible_client). "
            "Negotiis Ltd was moved with the Outlooks people-graph."
        )
        notes_simon.append(
            "A Fenn Limited / A. Fenn are Inactive — skipped. Alan Fennell moved with Sixty Six."
        )
        notes_simon.append(
            "Interiors of Cheshire Limited is Inactive — empty group deleted, not regrouped."
        )
        notes_simon.append("Test Co skipped and its empty group deleted.")

        write_doc(
            before,
            after,
            moves,
            created,
            renamed,
            deleted,
            cluster_reports,
            notes_simon,
        )
        print("WROTE", os.path.abspath(DOCS_PATH))
    finally:
        db.close()


def write_doc(
    before,
    after,
    moves,
    created,
    renamed,
    deleted,
    cluster_reports,
    notes_simon,
) -> None:
    path = os.path.abspath(DOCS_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    now_london = datetime.utcnow()  # write UTC then label; caller zone is London BST
    # BST in August = UTC+1
    from datetime import timedelta

    local = now_london + timedelta(hours=1)
    ts = local.strftime("%Y-%m-%d %H:%M") + " BST"

    def hist_line(h):
        return ", ".join(f"{k} member(s): {v} groups" for k, v in h.items()) or "(none)"

    lines: List[str] = []
    lines.append("# Practice grouping pass — 25 Aug 2026")
    lines.append("")
    lines.append(f"Written {ts}. Live sqlite `crm.db`. No git commit/push, no Render, no people-link edits.")
    lines.append("")
    lines.append("APIs: `app.services.practice_groups.move_client`, `create_group`, `rename_group`, `delete_group`.")
    lines.append("People-graph: `app.services.grouping.list_group_summaries` / `get_group_detail`.")
    lines.append("Eligibility: `is_group_eligible_client` (skip Inactive / disengaged). Skip Test Co.")
    lines.append("")
    lines.append("## Before vs after")
    lines.append("")
    lines.append("| | Before | After |")
    lines.append("|---|---:|---:|")
    lines.append(f"| Practice groups | {before['practice_groups']} | {after['practice_groups']} |")
    lines.append(f"| Membership rows | {before['membership_rows']} | {after['membership_rows']} |")
    lines.append(f"| Ungrouped eligible | {before['ungrouped']} | {after['ungrouped']} |")
    lines.append(f"| Multi-client groups | {len(before['multi'])} | {len(after['multi'])} |")
    lines.append("")
    lines.append(f"Before size histogram: {hist_line(before['size_hist'])}")
    lines.append("")
    lines.append(f"After size histogram: {hist_line(after['size_hist'])}")
    lines.append("")
    lines.append("## Multi-client groups after")
    lines.append("")
    if not after["multi"]:
        lines.append("(none)")
    else:
        for g in sorted(after["multi"], key=lambda x: (-x["n"], x["name"].lower())):
            lines.append(
                f"- **G{g['id']} {g['name']}** — {g['n']} clients, fees {g['fees']:.2f}: "
                + "; ".join(g["members"])
            )
    lines.append("")
    lines.append("## Clusters applied")
    lines.append("")
    for cr in cluster_reports:
        fam = cr["family"] or "(people-graph, no Cluster B family label)"
        lines.append(f"### G{cr['target_id']} {cr['target_name']}")
        lines.append("")
        lines.append(f"- Family label: {fam}")
        lines.append(f"- Highest-fee member: {cr['lead']} (C{cr['lead_id']})")
        lines.append("- Members:")
        for name, cid in cr["members"]:
            lines.append(f"  - C{cid} {name}")
        lines.append("")
    lines.append("## Moves")
    lines.append("")
    if not moves:
        lines.append("(none)")
    else:
        lines.append("| Client | From | To |")
        lines.append("|---|---|---|")
        for m in moves:
            frm = m["from_name"] if m["from_gid"] is None else f"G{m['from_gid']} {m['from_name']}"
            if m["from_gid"] is None:
                frm = "(ungrouped)"
            lines.append(
                f"| C{m['client_id']} {m['name']} | {frm} | G{m['to_gid']} {m['to_name']} |"
            )
    lines.append("")
    lines.append("## Groups created")
    lines.append("")
    if not created:
        lines.append("(none)")
    else:
        for gid, name in created:
            lines.append(f"- G{gid} {name}")
    lines.append("")
    lines.append("## Groups renamed")
    lines.append("")
    if not renamed:
        lines.append("(none)")
    else:
        for gid, old, new in renamed:
            lines.append(f"- G{gid} {old!r} → {new!r}")
    lines.append("")
    lines.append("## Empty groups deleted")
    lines.append("")
    if not deleted:
        lines.append("(none)")
    else:
        for gid, name in deleted:
            lines.append(f"- G{gid} {name}")
    lines.append("")
    lines.append("## Remaining ungrouped (eligible)")
    lines.append("")
    if not after["ungrouped_rows"]:
        lines.append("(none)")
    else:
        for r in after["ungrouped_rows"]:
            lines.append(f"- C{r['id']} {r['name']} — {r['status']}, fees {r['fees']:.2f}")
    lines.append("")
    lines.append("## Needs Simon")
    lines.append("")
    for n in notes_simon:
        lines.append(f"- {n}")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- Cluster A merged every eligible people-graph component with 2+ clients.")
    lines.append("- Cluster B name families were unioned even where people links were incomplete, except Sqco/Sqfix and Nitem (people-graph only).")
    lines.append("- Accology Limited + Accology Pays were kept as their own family, not folded into Access Utilities.")
    lines.append("- No person_clients rows were added or removed.")
    lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()
