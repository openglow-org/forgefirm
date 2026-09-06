# forgectrl serves HTTPS on 443 with a self-signed certificate through
# this library, so the https option (GnuTLS) is on; curl is the poky
# default (it builds the library's own test client). libgcrypt is a
# build-time dependency of the https option in the poky recipe;
# libmicrohttpd links it only for a GnuTLS older than 2.12, so it does
# not reach the rootfs. The GnuTLS build options are in
# conf/distro/forgefirm.conf.
PACKAGECONFIG = "curl https"
