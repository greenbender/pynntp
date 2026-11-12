#!/bin/sh
# Set up Debian/Ubuntu's inn2 package for the integration tests. Run it as
# root on a CI runner or a disposable test machine only: it changes INN's
# configuration, and the tests post articles.
#
#   sudo tests/integration/inn2/setup.sh
#   PYNNTP_TLS_CAFILE=/etc/news/pynntp-cert.pem uv run pytest tests/integration
#
# Result: INN on port 119, nnrpd with implicit TLS (a self-signed certificate
# for localhost) on 563, and localhost allowed to read, post, and use NEWNEWS
# and IHAVE. Safe to run again.
set -eu
export DEBIAN_FRONTEND=noninteractive
ETC=/etc/news

set_conf() {
    if grep -q "^$1:" $ETC/inn.conf; then
        sed -i "s|^$1:.*|$1: $2|" $ETC/inn.conf
    else
        echo "$1: $2" >> $ETC/inn.conf
    fi
}

# The package's postinst fails if the hostname doesn't resolve, or has no
# domain; INN then also makes Message-IDs it rejects itself.
if ! getent hosts "$(hostname)" >/dev/null; then
    echo "127.0.1.1 $(hostname).localdomain $(hostname)" >> /etc/hosts
fi
apt-get -qq update
if ! apt-get -qq install -y inn2 openssl >/dev/null; then
    set_conf domain localdomain
    dpkg --configure -a
fi
case "$(hostname -f 2>/dev/null)" in
*.*) ;;
*) grep -q '^domain:' $ETC/inn.conf || set_conf domain localdomain ;;
esac

# No load limits: CI runners and containers can be busy.
set_conf nnrpdloadlimit 0
set_conf doinnwatch false

# localhost must reach nnrpd, not innd as a feeding peer: the client may send
# STARTTLS before MODE READER.
sed -i '/^peer ME/,/^}/ s/^/#pynntp# /' $ETC/incoming.conf

# NEWNEWS (N) and IHAVE (I) for localhost.
sed -i '/^access "localhost"/,/^}/ s/^\(\s*access:\s*\)RPAN\?I\?\s*$/\1RPANI/' \
    $ETC/readers.conf

# TLS, with a self-signed certificate.
if [ ! -f $ETC/pynntp-cert.pem ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=localhost" \
        -keyout $ETC/pynntp-key.pem -out $ETC/pynntp-cert.pem 2>/dev/null
    chown news:news $ETC/pynntp-key.pem $ETC/pynntp-cert.pem
    chmod 600 $ETC/pynntp-key.pem
fi
set_conf tlscertfile $ETC/pynntp-cert.pem
set_conf tlskeyfile $ETC/pynntp-key.pem
set_conf tlscapath $ETC

systemctl restart inn2
sleep 3

# The groups the tests use; the package usually has them.
for group in local.general local.test; do
    if ! grep -q "^$group " /var/lib/news/active; then
        su -s /bin/sh news -c "/usr/lib/news/bin/ctlinnd newgroup $group y"
    fi
done

# nnrpd with implicit TLS on 563. It reads inn.conf only when it starts, so
# replace one left from an earlier run.
for pid in $(ss -ltnpH 'sport = :563' | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u); do
    kill "$pid" || true
done
sleep 1
/usr/lib/news/bin/nnrpd -D -p 563 -S
sleep 1
for port in 119 563; do
    if ! ss -ltnH "sport = :$port" | grep -q .; then
        echo "Nothing listens on port $port" >&2
        exit 1
    fi
done
/usr/lib/news/bin/innconfval -v
