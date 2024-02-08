try {
  require.resolve("@livestack/vault-dev-server");
  require("@livestack/vault-dev-server");
} catch (e) {
  console.warn("Vault dev server module not found. ");
}
