//! `lynch-chart SPEC.json [RUNS]`: renders a chart spec (as ui/fastchart.py sends it) and prints the timings of
//! each run as JSON lines. For benchmarking and debugging the renderer without Python.
fn main() {
    let mut args = std::env::args().skip(1);
    let Some(path) = args.next() else {
        eprintln!("usage: lynch-chart SPEC.json [RUNS]");
        std::process::exit(2);
    };
    let runs: usize = args.next().and_then(|r| r.parse().ok()).unwrap_or(1);
    let spec: lynch_chart::Spec = match std::fs::read(&path)
        .map_err(|e| e.to_string())
        .and_then(|b| serde_json::from_slice(&b).map_err(|e| e.to_string()))
    {
        Ok(s) => s,
        Err(e) => {
            eprintln!("{path}: {e}");
            std::process::exit(2);
        }
    };
    for _ in 0..runs {
        match lynch_chart::render(&spec) {
            Ok(t) => println!("{}", serde_json::to_string(&t).unwrap_or_default()),
            Err(e) => {
                eprintln!("{e}");
                std::process::exit(1);
            }
        }
    }
}
