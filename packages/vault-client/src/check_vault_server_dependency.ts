export function checkVaultServerDependency() {
  try {
    require.resolve("@livestack/vault-dev-server");
    const main = require("@livestack/vault-dev-server");
    main.launchVaultDevServer();
  } catch (e) {
    console.warn("Vault dev server module not found. ");
  }
}
