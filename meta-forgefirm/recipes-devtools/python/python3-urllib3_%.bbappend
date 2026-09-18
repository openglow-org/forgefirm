# SPDX-License-Identifier: MIT

# urllib3's optional pyOpenSSL/cryptography backend is not used here: the cloud
# client and the machine glue talk TLS through the standard library's ssl
# module (nothing imports OpenSSL or cryptography). Dropping the two runtime
# recommendations takes cryptography, pyOpenSSL, cffi, pycparser and ply
# (about 6 MB) off the rootfs.
RDEPENDS:${PN}:remove = "python3-cryptography python3-pyopenssl"
