# andai/portal/lib

`api.ts`: same-origin calls to andai-core and what a 401/403 answer means for navigation.
`session.ts`: the signed-in account for the app pages. `client-ip.cjs`: preloaded by the portal
server to rewrite X-Forwarded-For to the address it believes. `utils.ts`: `cn` for class names.
