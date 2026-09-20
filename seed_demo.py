"""Optional demo data for local testing.
Run: DEV_MODE=1 ALLOWED_TELEGRAM_IDS=1 python seed_demo.py
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from database import Base, SessionLocal, engine
from models import Employee, Order, Readiness

Base.metadata.create_all(bind=engine)
moscow = ZoneInfo("Europe/Moscow")
work_date = datetime.now(moscow).date() + timedelta(days=1)

people = [
    (1001,"Роман Иванов","brigadier",186,True),
    (1002,"Алексей Петров","brigadier",188,False),
    (2001,"Игорь Медведково","main",185,True),
    (2002,"Влад Рязанский","main",187,False),
    (3001,"Даниил ВДНХ","cashless",186,True),
    (3002,"Максим Ясенево","cashless",184,False),
    (4001,"Арсений Балашиха","reserve",188,True),
    (4002,"Серго Люблино","reserve",185,False),
]

with SessionLocal() as db:
    if not db.query(Employee).count():
        for tg,name,group,height,car in people:
            e=Employee(tg_id=tg,full_name=name,phone="+79990000000",group_code=group,height_cm=height,has_car=car)
            db.add(e); db.flush()
            db.add(Readiness(employee_id=e.id,work_date=work_date,status="ready",reported_at=datetime.utcnow()))
        db.add(Order(public_id="DEMO001",source="private",work_date=work_date,issue_time=datetime.strptime("11:00","%H:%M").time(),arrival_time=datetime.strptime("10:30","%H:%M").time(),deceased_name="Брешин (пример)",route="Морг → храм → кладбище",category="elite",team_size=4,agent_name="Арсений",agent_phone="+79990000001",status="new"))
        db.add(Order(public_id="DEMO002",source="gbu",work_date=work_date,issue_time=datetime.strptime("12:30","%H:%M").time(),arrival_time=datetime.strptime("12:00","%H:%M").time(),deceased_name="Соколов (ГБУ пример)",route="Морг → кладбище",category="standard",team_size=6,organization="ГБУ",status="new"))
        db.commit()
        print("Demo data created for",work_date)
    else:
        print("Database already has employees; nothing changed")
