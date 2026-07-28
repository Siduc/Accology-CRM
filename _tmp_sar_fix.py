from collections import Counter
from datetime import date

from app.database import SessionLocal
from app.models import Client, Job, Person
from app.models.sales import Service, ServicePrice
from app.models.service_fee import ServiceFee

db = SessionLocal()

statuses = Counter((p.person_status or "(none)") for p in db.query(Person).all())
print("person_status counts:")
for s, c in statuses.most_common(40):
    print(f"  {c:4} {s!r}")

people = db.query(Person).all()
print("\nstatus contains SA/SAR/tax/individual:")
for p in people:
    st = p.person_status or ""
    role = p.role or ""
    if any(
        x in (st + " " + role).lower()
        for x in ("sa", "sar", "tax", "individual", "self assess")
    ):
        print(
            f"  id={p.id} indiv={bool(p.is_individual_client)} "
            f"status={st!r} role={role!r} name={p.full_name!r}"
        )

print("\nServices:")
for s in db.query(Service).all():
    print(
        f"  id={s.id} name={s.name!r} "
        f"code={getattr(s, 'code', None)!r} "
        f"active={getattr(s, 'is_active', None)}"
    )

print("\nServiceFees (all codes sample):")
codes = Counter()
for f in db.query(ServiceFee).all():
    codes[f.service_code] += 1
for code, n in codes.most_common(20):
    print(f"  {n:3} {code!r}")

print("\nServiceFee rows for SA:")
for f in db.query(ServiceFee).all():
    if f.service_code and (
        "self" in f.service_code.lower()
        or f.service_code.upper() == "SA"
        or "assessment" in f.service_code.lower()
    ):
        print(f"  {f.service_code} year={f.year} fee={f.fee}")

pe = date(2026, 4, 5)
jobs = (
    db.query(Job)
    .filter(Job.type == "Self Assessment", Job.period_end == pe)
    .all()
)
print(f"\nSA jobs PE 2026-04-05: {len(jobs)}")
for j in jobs[:15]:
    print(f"  job={j.id} client={j.client_id} status={j.status} fee={j.fee}")

# people already individual
indiv = [p for p in people if p.is_individual_client]
print(f"\nAlready individual clients: {len(indiv)}")

db.close()
