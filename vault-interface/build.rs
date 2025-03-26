fn main() -> Result<(), Box<dyn std::error::Error>> {
    println!("cargo:rustc-check-cfg=cfg(debug_assert)");
    tonic_build::compile_protos("proto/stream.proto")?;
    napi_build::setup();
    Ok(())
}