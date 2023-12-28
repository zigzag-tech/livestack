doc to do
1. racing output condition
// output PSFactory: 9:07:00 msg1 (coming from input1, took 2 sec, send "v.data"), 
                      // 9:07:01 msg2 (coming from input:terminate, took 0.5 sec, send "v.terminate")

2. ZZJob
if (processedR) {
          await this.emitOutput(processedR);

          // signalEnd
          return processedR;
        } else {
          // signalEnd
        }

        else condition needs to handle observable case // might wanna double check, but theoretically this shouldn't be a concern as the await Promise for counting input observables should have handled this

3. Audio chunk
Robust test case where audio chunk files come in, each file gets sent to whisper server (pipe.sendInput), value returned from whisper server as transcript, transcript output emitted => processor = inputObservable OR while loop ; await callWhisperServer (audioChunk) (returning transcript)

parallel condition guaranteeing original input order

4. minor bug assistant clean up

Legacy server => use server (OR use openai whisper API)
sendInput, wait, emitOutput
(long running worker)
phrase complete?

const {sendInput} = pipe.waitForJobToOpenForBusiness(jobId)
setOpenForBusinessInRedis(true)
