// App-owned Harmony command handler. Preparation is executable; native unit
// transition remains refused until a separately qualified forward/reverse plan.
import { createHash } from 'node:crypto';
import { execFile } from 'node:child_process';
import { openSync, closeSync, fstatSync, readFileSync, lstatSync, writeFileSync, readdirSync } from 'node:fs';
import { constants } from 'node:fs';
import { isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
const digest = value => createHash('sha256').update(value).digest('hex');
const hex = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const refuse = reason => { throw new Error(`broker_handler_${reason}`); };
const exact = (value, fields) => value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).sort().join() === fields;

function readBoundFile(value, { privateFile = false } = {}) {
  if (!exact(value, 'path,sha256,size') || !isAbsolute(value.path) || !hex(value.sha256) || !Number.isSafeInteger(value.size) || value.size < 0) refuse('closed_file_descriptor');
  const fd = openSync(value.path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = fstatSync(fd);
    if (!info.isFile() || info.size !== value.size || privateFile && (info.uid !== process.getuid() || info.mode & 0o077)) refuse('file_custody');
    const bytes = readFileSync(fd);
    if (bytes.length !== value.size || digest(bytes) !== value.sha256) refuse('file_bytes_changed');
    return bytes;
  } finally { closeSync(fd); }
}

function run(executable, args, body) {
  return new Promise((resolvePromise, reject) => {
    const child = execFile(executable, args, { timeout: 20000, maxBuffer: 65536 }, (error, stdout) => {
      if (error) reject(new Error('broker_handler_control_failed_or_unknown'));
      else resolvePromise(stdout);
    });
    child.stdin.end(body ?? '');
  });
}

export async function runBrokerDeploymentHandler(configurationPath, configurationSha256, { requestPath, inputDirectory, outputDirectory }) {
  if (!hex(configurationSha256) || !isAbsolute(configurationPath ?? '') || !isAbsolute(requestPath ?? '') || !isAbsolute(inputDirectory ?? '') || !isAbsolute(outputDirectory ?? '')) refuse('fixed_installed_paths');
  const stat = lstatSync(configurationPath);
  const bytes = readBoundFile({ path: configurationPath, sha256: configurationSha256, size: stat.size }, { privateFile: true });
  const config = JSON.parse(bytes);
  if (!exact(config, 'archiveDigest,authorization,operation,requestedBy,runtime,target,version') || config.version !== 1 || config.target !== 'joe-hostd' || !/^machine:[^\s]{1,160}$/.test(config.requestedBy ?? '') || !hex(config.archiveDigest) || !['prepare', 'status'].includes(config.operation)) refuse('closed_registered_configuration');
  const requestBytes = readFileSync(requestPath);
  if (requestBytes.length > 8192) refuse('request_bound');
  const request = JSON.parse(requestBytes);
  if (!exact(request, 'action,digest,object,requested_by,source,target,version') || request.version !== 1 || request.action !== 'deploy' || request.target !== config.target || request.object !== 'site.tar.gz' || request.digest !== config.archiveDigest || !exact(request.requested_by, 'kind,principal') || request.requested_by.kind !== 'machine' || request.requested_by.principal !== config.requestedBy || request.source !== null && (typeof request.source !== 'string' || request.source.length > 128)) refuse('registered_deploy_request');
  const archive = readFileSync(join(inputDirectory, 'site.tar.gz'));
  if (digest(archive) !== config.archiveDigest) refuse('captured_archive_changed');
  if (!exact(config.authorization, 'bundle,configuration,keeperSourceSha256,node,principal') || config.authorization.principal !== config.requestedBy || !hex(config.authorization.keeperSourceSha256) || !exact(config.runtime, 'configuration,files,python,sourceRoot') || !isAbsolute(config.runtime.sourceRoot ?? '') || !Array.isArray(config.runtime.files) || !config.runtime.files.length) refuse('closed_source_runtime');
  const sourceInventory = () => {
    const paths = [];
    const visit = directory => {
      for (const entry of readdirSync(directory, { withFileTypes: true })) {
        const path = join(directory, entry.name);
        if (entry.isSymbolicLink()) refuse('source_alias');
        if (entry.isDirectory()) visit(path);
        else if (entry.isFile()) paths.push(path);
        else refuse('source_nonregular_entry');
        if (paths.length > 100000) refuse('source_inventory_bound');
      }
    };
    visit(config.runtime.sourceRoot);
    if (paths.sort().join('\n') !== config.runtime.files.map(file => file.path).sort().join('\n')) refuse('source_inventory_incomplete');
    for (const required of ['__init__.py','broker_retirement_cli.py','broker_retirement.py','broker_predecessor.py','provider_owner_probe.py']) {
      if (!paths.includes(join(config.runtime.sourceRoot,'livestack_node',required))) refuse('source_entry_missing');
    }
  };
  const bound = () => {
    sourceInventory();
    readBoundFile(config.authorization.node); readBoundFile(config.authorization.bundle);
    readBoundFile(config.authorization.configuration, { privateFile: true });
    readBoundFile(config.runtime.python); readBoundFile(config.runtime.configuration, { privateFile: true });
    for (const file of config.runtime.files) readBoundFile(file);
    readBoundFile({ path: configurationPath, sha256: configurationSha256, size: bytes.length }, { privateFile: true });
  };
  bound();
  // This is the actual pinned SDK client. The handler accepts no permission
  // receipt in its payload and no injectable authorizer or ambient credential.
  const permission = JSON.parse(await run(config.authorization.node.path, [config.authorization.bundle.path, config.authorization.configuration.path], JSON.stringify({ effect: config.operation })));
  bound();
  if (!exact(permission, 'authorityMachine,keeperSourceSha256,nonce,ok,permissionOnly,principal,requestSha256') || permission.ok !== true || permission.permissionOnly !== true || permission.principal !== config.requestedBy || permission.keeperSourceSha256 !== config.authorization.keeperSourceSha256 || !hex(permission.nonce) || !hex(permission.requestSha256)) refuse('canonical_permission_receipt');
  const launcher = 'import sys,runpy;sys.path.insert(0,sys.argv[1]);sys.argv=sys.argv[2:];runpy.run_module("livestack_node.broker_retirement_cli",run_name="__main__")';
  const result = JSON.parse(await run(config.runtime.python.path, ['-I', '-B', '-c', launcher, config.runtime.sourceRoot, 'broker-retirement', '--configuration', config.runtime.configuration.path, '--operation', config.operation]));
  bound();
  if (result.activationAllowed !== false || result.jobsCompleted === true || result.modelEffectsAllowed === true) refuse('diagnostic_claim_exceeded');
  const receipt = { version: 1, phase: 'broker-owner-preparation', sourceArchiveDigest: config.archiveDigest,
    permission, result, deploymentSucceeded: false, unitEffectsApplied: false, modelEffectsApplied: false };
  writeFileSync(join(outputDirectory, 'broker-owner-receipt.json'), JSON.stringify(receipt, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
  return receipt;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    if (process.argv.length !== 4) refuse('fixed_configuration_argv');
    await runBrokerDeploymentHandler(process.argv[2], process.argv[3], { requestPath: process.env.HARMONY_REQUEST, inputDirectory: process.env.HARMONY_INPUT_OBJECTS, outputDirectory: process.env.HARMONY_OUTPUT });
  } catch {
    console.error('broker_handler_refused');
    process.exitCode = 2;
  }
}
