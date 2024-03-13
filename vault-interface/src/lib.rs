use napi::Error;
use tonic::{transport::Server, Request, Response, Status};


pub mod livestack {
    tonic::include_proto!("livestack"); // The string specified here must match the proto package name
}


use livestack::stream_service_client::StreamServiceClient;
use napi_derive::napi;

#[napi]
pub async fn test_fn() -> Result<(), Error>{
    Ok(())
}