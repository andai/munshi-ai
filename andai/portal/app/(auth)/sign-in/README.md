# /sign-in

Email and password (POST /api/v1/auth/login); the stage andai-core answers decides where the
browser goes next: `/`, `/2fa` or `/2fa/setup`. Shows the rate-limit message on 429.
