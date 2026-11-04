
Activepieces: Dex Authentication Deployment


Objective

Deploy the already-patched `ghcr.io/enki-farm/activepieces:latest` image with Dex authentication through the stack's existing Envoy Gateway. The application patch trusts identity headers only from the Gateway policy; this repository manages the Kubernetes integration, Dex client and public webhook routing.

1. System Architecture Overview

```text
[ User Browser ] -> [ Envoy Gateway OIDC ] <-> [ Dex IdP ]
        |
       verified JWT claim mapping
        |
        v
        [ Activepieces Service ]
          /              \
   protected UI/API       public webhook route
          (identity headers stripped)
```

2. Infrastructure Configuration
Envoy Gateway is the existing public edge. No NGINX Ingress, oauth2-proxy, or extra reverse proxy is deployed.

### Dex Client

The existing `auth` component registers Dex client `activepieces` with callback
`https://ap.<domain>/oauth2/callback`. Its generated client secret is copied from
`dex/dex-secrets` into the addon-owned `activepieces/activepieces-oidc` Secret for
the Envoy policy. Install or upgrade `auth` before installing the addon.

### Gateway Routes

The `activepieces` HTTPRoute sends `https://ap.<domain>/` to the Activepieces
Service. Its `SecurityPolicy` performs Dex OIDC login and validates the forwarded
Dex access token as a JWT. Verified `email`, `name`, and `preferred_username`
claims become `X-Auth-Request-Email`, `X-Auth-Request-User`, and
`X-Auth-Request-Preferred-Username` for the patched application middleware.

The separate `activepieces-webhooks` HTTPRoute matches only
`/api/v1/webhooks/*`, the public webhook controller path in Activepieces 0.92.2.
It has no OIDC policy and removes proxy identity headers before forwarding to the
same Service. Do not add a broader unauthenticated route.

3. Activepieces Application Patch (Already In The Image)


3.1 Backend Header Authentication

Target Package: packages/server/api / Authentication Pipeline
The deployed image already contains this behavior: trusted `X-Auth-Request-*` headers
are used to provision and authenticate users, and the standard login UI is bypassed.
The following excerpt records the original design only; do not apply it to this repo.
```typescript
import { Request, Response, NextFunction } from 'express';
import { userService } from '../user/user-service';
import { authenticationService } from './authentication-service';
import { projectService } from '../project/project-service';

export const headerAuthMiddleware = async (req: Request, res: Response, next: NextFunction) => {
    // 1. Extract proxy headers injected by oauth2-proxy
    const email = req.header('x-auth-request-email');
    const fullName = req.header('x-auth-request-user') || req.header('x-auth-request-preferred-username') || 'Platform User';

    if (!email) {
        // If headers aren't present, allow the regular request pipeline to handle it (or reject if strict proxy mode is required)
        return next();
    }

    try {
        // 2. Query Activepieces database for the user record
        let user = await userService.getByEmail(email);

        // 3. Auto-provision the user if they do not exist
        if (!user) {
            user = await userService.create({
                email: email,
                firstName: fullName.split(' ')[0] || 'Platform',
                lastName: fullName.split(' ').slice(1).join(' ') || 'User',
                password: 'COMPROMISED_RANDOM_STRING_NOT_USED_IN_HEADER_AUTH_' + Math.random().toString(36),
                trackEvents: false
            });

            // 4. Auto-provision a default project for the newly created user
            await projectService.create({
                displayName: `${fullName}'s Project`,
                ownerId: user.id,
            });
        }

        // 5. Generate a native Activepieces JWT token using the internal system
        const token = await authenticationService.generateToken(user);

        // 6. Inject the generated token into the standard Authorization Header for subsequent framework operations
        req.headers['authorization'] = `Bearer ${token}`;
        
        // Expose user session data to downstream context loaders
        (req as any).user = user;

        next();
    } catch (error) {
        console.error('Error handling headless header authentication:', error);
        res.status(500).json({ error: 'Internal Authentication Error' });
    }
};
```
Use code with caution.

3.2 Backend and Frontend Integration

The middleware, session provisioning, and login UI bypass are already included in
the deployed image. The Kubernetes deployment enables them with
`AP_PROXY_AUTH_ENABLED=true` and
`AP_PROXY_AUTH_BOOTSTRAP_ADMIN_EMAIL=marco@enki.swiss`; no changes to
`packages/server` or `packages/ui/core` are part of this stack patch.

Kubernetes Integration Instructions

1. Scope: Modify this stack's Kubernetes/CLI addon integration and documentation only. The application patch is already in `ghcr.io/enki-farm/activepieces:latest`.
2. Environment: Set `AP_PROXY_AUTH_ENABLED=true` and `AP_PROXY_AUTH_BOOTSTRAP_ADMIN_EMAIL=marco@enki.swiss`.
3. Database Interactions: Utilize the existing TypeORM repositories via the built-in userService and projectService abstractions to avoid raw SQL generation.
4. Token Generation: The image uses the native signing key configured via `AP_JWT_SECRET`; do not add another token issuer.