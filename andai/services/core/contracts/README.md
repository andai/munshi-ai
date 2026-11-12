# andai-core contracts

Versioned formats andai-core publishes: `export-bundle.v1`, the data export bundle (OPS-02). Each
`X.vN` directory holds its JSON Schema, synthetic goldens and `validate.mjs` (`gate:schema`), and is
sealed in the root `contracts.seal.json` (`gate:contract-version`).
