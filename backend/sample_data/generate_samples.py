"""生成两个站点的样例计量文件（CSV），覆盖全部核算规则场景。

站点 S01 上海（Asia/Shanghai，UTC+8）：正常核对、跨时段实测/估算拆分、断网补报修订、
公共照明（已确认）、未解释差额在阈值内可确认。
站点 S02 乌鲁木齐（Asia/Urumqi，UTC+6）：设备更换桥接、读数回退无依据挂起、
重复订单、时段冲突、待补订单、未确认公共负荷、未解释差额超阈值挂起。
"""
from pathlib import Path

BASE = Path(__file__).parent


def write(path: Path, header: str, rows: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(header + "\n")
        f.write("\n".join(rows) + "\n")


READING_HEADER = "record_no,meter_no,device_id,reading_time,reading_kwh,is_correction"
ORDER_HEADER = (
    "record_no,order_no,device_id,start_time,end_time,start_reading,end_reading,reported_energy"
)
PUBLIC_HEADER = "record_no,load_name,start_time,end_time,energy_kwh,confirmed"
REPL_HEADER = (
    "record_no,meter_no,effective_time,old_final_reading,new_initial_reading,evidence_note"
)
TARIFF_HEADER = (
    "record_no,tariff_name,effective_date,end_date,segment_name,start_hour,end_hour,price"
)


def gen_shanghai():
    d = BASE / "S01"
    # 物业总表：周读数，合计 2600 kWh
    write(d / "master_readings.csv", READING_HEADER, [
        "MR-SH-001,M-SH-01,,2026-10-01 00:00,5000,false",
        "MR-SH-002,M-SH-01,,2026-10-08 00:00,5620,false",
        "MR-SH-003,M-SH-01,,2026-10-15 00:00,6280,false",
        "MR-SH-004,M-SH-01,,2026-10-22 00:00,6920,false",
        "MR-SH-005,M-SH-01,,2026-11-01 00:00,7600,false",
    ])
    # 设备表：DEV-A / DEV-B，含 10-02 10:00 的中间计量点（用于跨时段实测拆分）
    write(d / "device_readings.csv", READING_HEADER, [
        "DR-SH-A1,M-A,DEV-A,2026-10-01 00:00,0,false",
        "DR-SH-A2,M-A,DEV-A,2026-10-02 10:00,25,false",
        "DR-SH-A3,M-A,DEV-A,2026-10-08 00:00,300,false",
        "DR-SH-A4,M-A,DEV-A,2026-10-15 00:00,620,false",
        "DR-SH-A5,M-A,DEV-A,2026-10-22 00:00,900,false",
        "DR-SH-A6,M-A,DEV-A,2026-11-01 00:00,1250,false",
        "DR-SH-B1,M-B,DEV-B,2026-10-01 00:00,0,false",
        "DR-SH-B2,M-B,DEV-B,2026-10-08 00:00,250,false",
        "DR-SH-B3,M-B,DEV-B,2026-10-15 00:00,500,false",
        "DR-SH-B4,M-B,DEV-B,2026-10-22 00:00,760,false",
        "DR-SH-B5,M-B,DEV-B,2026-11-01 00:00,1000,false",
    ])
    # 订单首版：O-1009 断网期间只有上报电量、无起止读数
    write(d / "orders_v1.csv", ORDER_HEADER, [
        "OR-SH-001,O-1001,DEV-A,2026-10-02 09:00,2026-10-02 11:00,10,40,30",
        "OR-SH-002,O-1002,DEV-A,2026-10-03 21:00,2026-10-03 23:00,40,60,20",
        "OR-SH-003,O-1003,DEV-A,2026-10-05 02:00,2026-10-05 04:00,60,85,25",
        "OR-SH-004,O-1004,DEV-A,2026-10-10 14:00,2026-10-10 16:00,120,150,30",
        "OR-SH-005,O-1005,DEV-A,2026-10-17 07:30,2026-10-17 09:30,350,380,27",
        "OR-SH-006,O-1006,DEV-A,2026-10-25 19:00,2026-10-25 20:00,700,720,20",
        "OR-SH-007,O-1007,DEV-B,2026-10-06 13:00,2026-10-06 15:00,30,55,25",
        "OR-SH-008,O-1008,DEV-B,2026-10-20 11:30,2026-10-20 13:30,300,330,30",
        "OR-SH-009,O-1009,DEV-B,2026-10-27 08:00,2026-10-27 09:00,,,40",
    ])
    # 补报文件：同号 OR-SH-009 补齐起止读数（作业前导入 => 修订生效）
    write(d / "orders_v2_backfill.csv", ORDER_HEADER, [
        "OR-SH-009,O-1009,DEV-B,2026-10-27 08:00,2026-10-27 09:00,500,540,40",
    ])
    # 作业采用后到达的更正（演示 blocked_adopted，测试脚本中使用）
    write(d / "orders_v3_after_adopt.csv", ORDER_HEADER, [
        "OR-SH-003,O-1003,DEV-A,2026-10-05 02:00,2026-10-05 04:00,60,86,26",
    ])
    # 公共照明：已确认，整月 320 kWh
    write(d / "public_loads.csv", PUBLIC_HEADER, [
        "PL-SH-001,地下车库公共照明,2026-10-01 00:00,2026-11-01 00:00,320,true",
    ])
    # 电价：两个版本，10-15 调价
    write(d / "tariffs.csv", TARIFF_HEADER, [
        "TAR-SH-Q3,上海三季度电价,2026-09-01,2026-10-15,valley,0,6,0.3500",
        "TAR-SH-Q3,上海三季度电价,2026-09-01,2026-10-15,peak,6,10,1.0500",
        "TAR-SH-Q3,上海三季度电价,2026-09-01,2026-10-15,flat,10,18,0.7500",
        "TAR-SH-Q3,上海三季度电价,2026-09-01,2026-10-15,peak,18,22,1.0500",
        "TAR-SH-Q3,上海三季度电价,2026-09-01,2026-10-15,valley,22,24,0.3500",
        "TAR-SH-Q4,上海四季度电价,2026-10-15,,valley,0,6,0.3700",
        "TAR-SH-Q4,上海四季度电价,2026-10-15,,peak,6,10,1.0700",
        "TAR-SH-Q4,上海四季度电价,2026-10-15,,flat,10,18,0.7700",
        "TAR-SH-Q4,上海四季度电价,2026-10-15,,peak,18,22,1.0700",
        "TAR-SH-Q4,上海四季度电价,2026-10-15,,valley,22,24,0.3700",
    ])


def gen_urumqi():
    d = BASE / "S02"
    write(d / "master_readings.csv", READING_HEADER, [
        "MR-XJ-001,M-XJ-01,,2026-10-01 00:00,8000,false",
        "MR-XJ-002,M-XJ-01,,2026-10-08 00:00,8800,false",
        "MR-XJ-003,M-XJ-01,,2026-10-15 00:00,9600,false",
        "MR-XJ-004,M-XJ-01,,2026-10-22 00:00,10400,false",
        "MR-XJ-005,M-XJ-01,,2026-11-01 00:00,11200,false",
    ])
    # M-C 10-15 换表（旧表尾 800，新表起 50）；M-E 10-15 读数回退且无换表依据
    write(d / "device_readings.csv", READING_HEADER, [
        "DR-XJ-C1,M-C,DEV-C,2026-10-01 00:00,0,false",
        "DR-XJ-C2,M-C,DEV-C,2026-10-08 00:00,400,false",
        "DR-XJ-C3,M-C,DEV-C,2026-10-15 00:00,800,false",
        "DR-XJ-C4,M-C,DEV-C,2026-10-15 00:00,50,false",
        "DR-XJ-C5,M-C,DEV-C,2026-10-22 00:00,450,false",
        "DR-XJ-C6,M-C,DEV-C,2026-11-01 00:00,900,false",
        "DR-XJ-D1,M-D,DEV-D,2026-10-01 00:00,0,false",
        "DR-XJ-D2,M-D,DEV-D,2026-10-08 00:00,300,false",
        "DR-XJ-D3,M-D,DEV-D,2026-10-15 00:00,600,false",
        "DR-XJ-D4,M-D,DEV-D,2026-10-22 00:00,900,false",
        "DR-XJ-D5,M-D,DEV-D,2026-11-01 00:00,1200,false",
        "DR-XJ-E1,M-E,DEV-E,2026-10-01 00:00,0,false",
        "DR-XJ-E2,M-E,DEV-E,2026-10-08 00:00,100,false",
        "DR-XJ-E3,M-E,DEV-E,2026-10-15 00:00,80,false",
    ])
    write(d / "replacements.csv", REPL_HEADER, [
        "RP-XJ-001,M-C,2026-10-15 00:00,800,50,旧表通讯模块烧毁，工单 WO-20261015-07 换新表",
    ])
    write(d / "orders.csv", ORDER_HEADER, [
        # 实测订单
        "OR-XJ-001,O-2001,DEV-C,2026-10-06 08:00,2026-10-06 09:00,100,140,40",
        "OR-XJ-002,O-2002,DEV-D,2026-10-12 12:00,2026-10-12 14:00,200,250,50",
        # 跨时段无中间计量、无起止读数 => 估算项
        "OR-XJ-003,O-2003,DEV-D,2026-10-18 21:00,2026-10-18 23:00,,,36",
        # 无任何依据 => 待补
        "OR-XJ-004,O-2004,DEV-C,2026-10-26 10:00,2026-10-26 11:00,,,0",
        # 部分重叠 => 时段冲突，运营确认前均不计
        "OR-XJ-005,O-2005,DEV-X,2026-10-09 13:00,2026-10-09 15:00,,,20",
        "OR-XJ-006,O-2005B,DEV-X,2026-10-09 14:00,2026-10-09 16:00,,,15",
        # 完全同时段 => 重复上报，只计一次
        "OR-XJ-007,O-2006,DEV-Y,2026-10-11 09:00,2026-10-11 10:00,,,12",
        "OR-XJ-008,O-2006D,DEV-Y,2026-10-11 09:00,2026-10-11 10:00,,,12",
    ])
    # 公共照明未确认 => 生成物业核对事项，不计入账单
    write(d / "public_loads.csv", PUBLIC_HEADER, [
        "PL-XJ-001,站区路灯与标识照明,2026-10-01 00:00,2026-11-01 00:00,200,false",
    ])
    write(d / "tariffs.csv", TARIFF_HEADER, [
        "TAR-XJ-Q4,乌鲁木齐四季度电价,2026-09-01,,valley,0,8,0.3000",
        "TAR-XJ-Q4,乌鲁木齐四季度电价,2026-09-01,,flat,8,10,0.7000",
        "TAR-XJ-Q4,乌鲁木齐四季度电价,2026-09-01,,peak,10,13,1.0000",
        "TAR-XJ-Q4,乌鲁木齐四季度电价,2026-09-01,,flat,13,19,0.7000",
        "TAR-XJ-Q4,乌鲁木齐四季度电价,2026-09-01,,peak,19,23,1.0000",
        "TAR-XJ-Q4,乌鲁木齐四季度电价,2026-09-01,,valley,23,24,0.3000",
    ])


def gen_backfill():
    """跨月回补演示数据（在 10 月账单确认之后导入，见 README 第十节）。

    01 纠正的总表读数、02 迟到订单、03 撤销的重复订单、04 电价更正；
    05 必须在 orders_v3_after_adopt.csv（产生 rev2）之后导入，
    其声明 replaces_revision_no=1 而实际前序应为 rev2，用于演示缺链/断链挂起。
    """
    d = BASE / "S01" / "backfill"
    write(d / "01_corrected_master_reading.csv", READING_HEADER, [
        "MR-SH-003,M-SH-01,,2026-10-15 00:00,6290,true",
    ])
    write(d / "02_late_order.csv", ORDER_HEADER, [
        "OR-SH-LATE1,O-1999,DEV-A,2026-10-12 13:00,2026-10-12 14:00,200,220,20",
    ])
    write(d / "03_revoke_duplicate_order.csv",
          ORDER_HEADER + ",is_revocation", [
        "OR-SH-002,O-1002,DEV-A,2026-10-03 21:00,2026-10-03 23:00,40,60,20,true",
    ])
    write(d / "04_tariff_correction.csv", TARIFF_HEADER, [
        "TAR-SH-Q3,上海三季度电价,2026-09-01,2026-10-15,valley,0,6,0.3600",
        "TAR-SH-Q3,上海三季度电价,2026-09-01,2026-10-15,peak,6,10,1.0600",
        "TAR-SH-Q3,上海三季度电价,2026-09-01,2026-10-15,flat,10,18,0.7600",
        "TAR-SH-Q3,上海三季度电价,2026-09-01,2026-10-15,peak,18,22,1.0600",
        "TAR-SH-Q3,上海三季度电价,2026-09-01,2026-10-15,valley,22,24,0.3600",
    ])
    write(d / "05_broken_chain_order.csv",
          ORDER_HEADER + ",replaces_revision_no", [
        "OR-SH-003,O-1003,DEV-A,2026-10-05 02:00,2026-10-05 04:00,60,87,27,1",
    ])


if __name__ == "__main__":
    gen_shanghai()
    gen_urumqi()
    gen_backfill()
    print(f"样例文件已生成于 {BASE}")
