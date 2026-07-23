#!/usr/bin/env bash
# Regenerate benchmark_data/subsets/*.json from their parent benchmark_data/*.json
# inputs via scripts/subset.py. Run from the repo root.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

OUT=benchmark_data/subsets
IN=benchmark_data

subset() {
  python3 scripts/subset.py "$IN/$1.json" "$OUT/$2.json" "${@:3}"
}

subset aave-v3-origin-3.6.0 aave-deploy-batched-3.6.0 \
  scripts/DeployAaveV3MarketBatched.sol

subset aave-v3-origin-3.6.0 aave-librarypreCompile-3.6.0 \
  scripts/misc/LibraryPreCompileOne.sol scripts/misc/LibraryPreCompileTwo.sol

subset aave-v3-origin-3.6.0 aave-pool-3.6.0 \
  src/contracts/instances/PoolInstance.sol \
  src/contracts/instances/ATokenInstance.sol \
  src/contracts/instances/VariableDebtTokenInstance.sol \
  src/contracts/instances/PoolConfiguratorInstance.sol \
  src/contracts/instances/L2PoolInstance.sol \
  src/contracts/instances/ATokenWithDelegationInstance.sol \
  src/contracts/instances/VariableDebtTokenMainnetInstanceGHO.sol

subset contracts-bedrock-7.0.0 bedrock-cannon-7.0.0 \
  src/cannon/MIPS64.sol src/cannon/PreimageOracle.sol

subset contracts-bedrock-7.0.0 bedrock-deploy-scripts-7.0.0 \
  scripts/deploy/Deploy.s.sol scripts/deploy/VerifyOPCM.s.sol scripts/L2Genesis.s.sol

subset contracts-bedrock-7.0.0 bedrock-interfaces-7.0.0 \
  src/L1/L1CrossDomainMessenger.sol src/L1/L1StandardBridge.sol \
  src/L1/SystemConfig.sol src/L1/SuperchainConfig.sol src/L1/L1ERC721Bridge.sol \
  src/dispute/DisputeGameFactory.sol src/dispute/AnchorStateRegistry.sol \
  src/L2/L2ContractsManager.sol src/L2/L2ToL2CrossDomainMessenger.sol \
  src/L2/GasPriceOracle.sol src/L2/L2StandardBridge.sol \
  src/periphery/drippie/Drippie.sol src/L2/L1Block.sol src/L2/CrossL2Inbox.sol

subset contracts-bedrock-7.0.0 bedrock-l2contractsmanager-7.0.0 \
  src/L2/L2ContractsManager.sol

subset contracts-bedrock-7.0.0 bedrock-nutbundle-7.0.0 \
  scripts/upgrade/GenerateNUTBundle.s.sol

subset openzeppelin-5.6.1 openzeppelin-timelock-5.6.1 \
  contracts/governance/TimelockController.sol contracts/metatx/ERC2771Forwarder.sol \
  contracts/access/manager/AccessManager.sol contracts/token/ERC6909/ERC6909.sol \
  contracts/proxy/transparent/TransparentUpgradeableProxy.sol \
  contracts/finance/VestingWallet.sol contracts/proxy/beacon/BeaconProxy.sol \
  contracts/proxy/beacon/UpgradeableBeacon.sol contracts/proxy/transparent/ProxyAdmin.sol \
  contracts/proxy/ERC1967/ERC1967Proxy.sol

subset seaport-1.6 seaport-core-1.6 \
  contracts/Seaport.sol contracts/conduit/Conduit.sol contracts/conduit/ConduitController.sol \
  contracts/helpers/SeaportRouter.sol contracts/helpers/order-validator/SeaportValidator.sol

subset seaport-1.6 seaport-referenceconsideration-1.6 \
  reference/ReferenceConsideration.sol

subset solady-0.1.26 solady-fixedpointmath-0.1.26 \
  test/FixedPointMathLib.t.sol

subset solady-0.1.26 solady-libstring-0.1.26 \
  test/LibString.t.sol test/Base64.t.sol test/CREATE3.t.sol

subset v4-core-4.0.0 v4-poolmanager-4.0.0 \
  src/PoolManager.sol src/test/PoolSwapTest.sol src/test/PoolModifyLiquidityTest.sol \
  src/test/PoolDonateTest.sol src/test/PoolClaimsTest.sol src/test/Fuzzers.sol \
  src/test/CustomCurveHook.sol src/test/DynamicFeesTestHook.sol \
  src/test/DeltaReturningHook.sol src/test/FeeTakingHook.sol src/test/LPFeeTakingHook.sol \
  src/test/PoolNestedActionsTest.sol src/test/SwapRouterNoChecks.sol

# bedrock-analysis-7.0.0: parse/analyze only, no codegen -- keep every source
# (all top-level dirs as prefix roots) then blank outputSelection so evmasm
# and ir pipelines are identical.
subset contracts-bedrock-7.0.0 bedrock-analysis-7.0.0 \
  src/ test/ interfaces/ scripts/ lib/
python3 -c "
import json
path = '$OUT/bedrock-analysis-7.0.0.json'
with open(path) as f:
    d = json.load(f)
d['settings']['outputSelection'] = {}
with open(path, 'w') as f:
    json.dump(d, f)
"
