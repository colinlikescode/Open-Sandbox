# @opensandbox/sdk

Optional native TypeScript client for OpenSandbox. For existing agent applications,
use the official `e2b` package and the repository's [setup instructions](../../instructions.md).

Build this package from the checkout:

```bash
npm ci
npm run build
```

```typescript
import { OpenSandbox } from '@opensandbox/sdk'

const client = new OpenSandbox({
  endpoint: process.env.OPENSANDBOX_ENDPOINT,
  apiKey: process.env.OPENSANDBOX_API_KEY,
})
const sandbox = await client.create({ cpu: 1, memory: '1Gi', timeout: 300 })
try {
  console.log((await sandbox.exec('printf hello')).stdout)
} finally {
  await sandbox.destroy()
}
```

Node 22+ is required. Trust the head's CA with `NODE_EXTRA_CA_CERTS` before starting
Node. The client always connects to the remote head and never starts a local
control plane. This package is built locally; setup does not publish it.

`npm run test:e2e` runs the official E2B TypeScript compatibility scenario against
an installed cluster configured through the `E2B_*` environment variables.
