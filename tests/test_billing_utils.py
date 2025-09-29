import math
import pytest
from services import billing as B


def test_canonical_job_id_variants():
    assert B.canonical_job_id("12345") == "12345"
    assert B.canonical_job_id("12345.batch") == "12345"
    assert B.canonical_job_id("12345.0") == "12345"
    assert B.canonical_job_id("  6789.step_1  ") == "6789"
    assert B.canonical_job_id("") == ""


def test_hms_to_hours():
    assert B.hms_to_hours("00:30:00") == 0.5
    assert B.hms_to_hours("01:00:00") == 1.0
    assert B.hms_to_hours("1-00:00:00") == 24.0
    assert pytest.approx(B.hms_to_hours("00:00:30.500"), 1e-6) == 30.5/3600
    assert B.hms_to_hours(None) == 0.0
    assert B.hms_to_hours("bad") == 0.0  # defensive


def test_extract_mem_cpu_gpu():
    # mem
    assert B.extract_mem_gb("mem=4G,cpu=8") == 4.0
    assert B.extract_mem_gb("mem=512M,cpu=8") == 0.5
    assert B.extract_mem_gb("cpu=8") == 0.0
    # cpu
    assert B.extract_cpu_count("cpu=16,gres/gpu=1") == 16
    assert B.extract_cpu_count("") == 0
    # gpu
    assert B.extract_gpu_count("gres/gpu=2,cpu=4") == 2
    assert B.extract_gpu_count("cpu=8") == 0
