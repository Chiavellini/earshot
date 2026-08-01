//! Architecture governance is executable: the node registry, path ownership,
//! contract graph, and node documents must stay mutually consistent.

use std::path::PathBuf;
use std::process::Command;

#[test]
fn node_contract_registry_is_consistent() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let output = Command::new("python3")
        .arg("scripts/doc-lint.py")
        .current_dir(&root)
        .output()
        .expect("python3 is required by the repository's operational tooling");

    assert!(
        output.status.success(),
        "architecture contract lint failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr),
    );
}
