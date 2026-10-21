import sys
sys.path.insert(0, '.')
from scratch.test_pipeline_run import run_simulation

print("=== 3-Month 2.0R ===")
run_simulation(2.0, 57)

print("\n=== 3-Month 2.25R ===")
run_simulation(2.25, 57)

print("\n=== 6-Month 2.0R ===")
run_simulation(2.0, 122)

print("\n=== 6-Month 2.25R ===")
run_simulation(2.25, 122)
