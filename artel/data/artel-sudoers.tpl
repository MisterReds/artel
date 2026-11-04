# Artel: lets @USER@ run the root wrapper (and nothing else) without a password. Installed by install.sh with consent.
Defaults!/usr/local/sbin/artel-root env_reset, !setenv
@USER@ ALL=(root) NOPASSWD: /usr/local/sbin/artel-root *
