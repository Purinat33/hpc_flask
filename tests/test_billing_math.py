import pandas as pd
from services.billing import compute_costs


def test_compute_costs_uses_elapsed_and_tres(monkeypatch):
    # Rates for tier detection—if compute_costs uses your rates store, pin it
    from models import rates_store
    monkeypatch.setattr(rates_store, "load_rates", lambda: {
        "mu": {"cpu": 1.0, "gpu": 5.0, "mem": 0.5}
    })

    df = pd.DataFrame([{
        "User": "x", "JobID": "42",
        # 2h wall, 4h totalCPU (2 cores)
        "Elapsed": "02:00:00", "TotalCPU": "04:00:00",
        "ReqTRES": "cpu=2,mem=8G,gres/gpu=1",            # 2 CPU, 1 GPU, 8 GB
        "State": "COMPLETED"
    }])

    out = compute_costs(df)

    # Derived fields exist
    assert {"CPU_Core_Hours", "GPU_Hours",
            "Mem_GB_Hours", "Cost (฿)"} <= set(out.columns)

    row = out.iloc[0]
    # 2h * 2 cores = 4 core-hours
    assert abs(row["CPU_Core_Hours"] - 4.0) < 1e-6
    # 2h * 1 GPU
    assert abs(row["GPU_Hours"] - 2.0) < 1e-6
    # 2h * 8 GB = 16 GB-hours
    assert abs(row["Mem_GB_Hours"] - 16.0) < 1e-6

    # Cost check with rates cpu=1, gpu=5, mem=0.5:
    # CPU: 4 * 1  = 4
    # GPU: 2 * 5  = 10
    # MEM: 16 * .5= 8
    # total = 22
    assert abs(row["Cost (฿)"] - 22.0) < 1e-6
