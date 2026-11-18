# Load tests (Gates 15-16)
Not executed at 50K VUs in this repo: that needs >= 10 k6 injectors / 1 JMeter controller + 10 workers, a GPU reranker pool,
Redis Cluster and a vLLM pool (finaldesign.md §6 capacity model). KPI-31..36 stay NOT_MEASURED until a real run is imported.

```
# smoke (what was run locally): k6 run -e SMOKE=1 load_tests/k6_50k_load.js
# full:  k6 run load_tests/k6_50k_load.js            (distribute with --execution-segment across injectors)
# JMeter: jmeter -n -t load_tests/jmeter_50k_plan.jmx -R worker1,worker2,...,worker10 -l results_50k.jtl -e -o dashboard_50k/
# then:  python3 tools/import_load_results.py k6_summary.json   -> evals/results/load.json (read by the scorecard)
```
The JMeter faithfulness assertion is applied to ANSWERED responses only (abstentions/pending legitimately have null faithfulness).
