fn main() -> Result<(), Box<dyn std::error::Error>> {
    tonic_build::compile_protos("proto/stream.proto")?;
    napi_build::setup();
    Ok(())
}