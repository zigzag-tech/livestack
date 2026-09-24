//! `livestack-policy`: the Jingway replay CLI over livestack's families
//! (`families`, `artifact validate|hash`, `replay`, `selfcheck`).
fn main() {
    jingway_policy::cli::run_cli(&livestack_policy_family::LivestackFamilies)
}
