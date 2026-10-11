import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { cpSync, mkdtempSync, mkdirSync, readdirSync, readFileSync, writeFileSync, statSync, realpathSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join, resolve } from 'node:path';
import { runBrokerDeploymentHandler } from '../deploy-handler.mjs';
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const record = path => ({ path, sha256: hash(readFileSync(path)), size: statSync(path).size });

test('actual native status persists honest preparation; malformed source/receipt refuse (CPU synthetic authority)', async () => {
  const root = mkdtempSync('/home/ubuntu/.cache/broker-handler-cpu-');
  const source = join(root, 'source'); mkdirSync(source);
  cpSync(resolve('node-py/livestack_node'), join(source, 'livestack_node'), { recursive: true });
  const files = [];
  const walk = path => { for (const entry of readdirSync(path, { withFileTypes: true })) { const full = join(path, entry.name); if (entry.isDirectory()) walk(full); else files.push(record(full)); } };
  walk(source);
  const retained = [];
  for (const role of ['source','unit','configuration','operations','ledger','lease-snapshot']) {
    const path=join(root,'retained-'+role);writeFileSync(path,'synthetic retained input');retained.push({...record(path),role,unit:'livestack-hostd.service'});
  }
  const unit={unit:'livestack-hostd.service',role:'broker',unitSha256:'a'.repeat(64),bootId:'a08ce6fd-f798-4451-a054-797216ed9748',invocationId:'b'.repeat(32),mainPid:123,startTicks:456,controlGroup:'/system.slice/livestack-hostd.service',acceptedWork:'unknown',impact:'operator-aborted-unknown-result'};
  const nativePath=join(root,'native.json');
  writeFileSync(nativePath,JSON.stringify({version:1,plan:{version:1,mode:'abort',units:[unit],retainedFiles:retained.map(({size,...row})=>({...row,size}))},registeredInventory:retained,journalPath:join(root,'journal.sqlite')}),{mode:0o600});
  const principal='machine:synthetic-broker-fixture', keeper='c'.repeat(64);
  const authPath=join(root,'synthetic-authorizer.mjs');
  // This deliberately does not qualify canonical permission: real PG transport
  // tests live in ZZOPS. It qualifies handler plumbing + native SQLite only.
  writeFileSync(authPath,`console.log(JSON.stringify({ok:true,permissionOnly:true,principal:${JSON.stringify(principal)},keeperSourceSha256:${JSON.stringify(keeper)},nonce:'${'d'.repeat(64)}',requestSha256:'${'e'.repeat(64)}',authorityMachine:'synthetic-fixture'}));`);
  const authConfig=join(root,'auth.json');writeFileSync(authConfig,'{}',{mode:0o600});
  const python=realpathSync(execFileSync('python3',['-c','import sys;print(sys.executable)'],{encoding:'utf8'}).trim());
  const input=join(root,'input'),output=join(root,'output');mkdirSync(input);mkdirSync(output);
  writeFileSync(join(input,'site.tar.gz'),'synthetic source');
  const archiveDigest=hash(readFileSync(join(input,'site.tar.gz')));
  const requestPath=join(root,'request.json');
  const request={version:1,action:'deploy',target:'joe-hostd',requested_by:{principal,kind:'machine'},source:null,object:'site.tar.gz',digest:archiveDigest};
  writeFileSync(requestPath,JSON.stringify(request));
  const config={version:1,target:'joe-hostd',requestedBy:principal,archiveDigest,operation:'status',authorization:{node:record(process.execPath),bundle:record(authPath),configuration:record(authConfig),keeperSourceSha256:keeper,principal},runtime:{python:record(python),sourceRoot:source,files,configuration:record(nativePath)}};
  const configPath=join(root,'handler.json');writeFileSync(configPath,JSON.stringify(config),{mode:0o600});
  const receipt=await runBrokerDeploymentHandler(configPath,hash(readFileSync(configPath)),{requestPath,inputDirectory:input,outputDirectory:output});
  assert.equal(receipt.deploymentSucceeded,false);assert.equal(receipt.unitEffectsApplied,false);assert.equal(receipt.result.activationAllowed,false);
  const original = readFileSync(configPath);
  writeFileSync(configPath,JSON.stringify({...config,runtime:{...config.runtime,files:files.slice(1)}}));
  await assert.rejects(runBrokerDeploymentHandler(configPath,hash(readFileSync(configPath)),{requestPath,inputDirectory:input,outputDirectory:output}),/source_inventory_incomplete/);
  writeFileSync(configPath,original);
  writeFileSync(requestPath,JSON.stringify({...request,permission:{ok:true}}));
  await assert.rejects(runBrokerDeploymentHandler(configPath,hash(original),{requestPath,inputDirectory:input,outputDirectory:output}),/registered_deploy_request/);
});
