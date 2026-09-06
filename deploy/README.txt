NEXORA BOOKS ON A SERVER
========================

What this is, and what it is not
-------------------------------

THIS IS: one organisation's books, on a server, reachable at a subdomain of
your own. Your staff open a browser from anywhere in the world and sign in.
No installer, no VPN, no code-signing certificate, no "which computer has the
real books". It solves the remote-staff problem completely.

THIS IS NOT: a service you can sell hosted accounts on — yet. Keeping one
company's data away from another's is already done and done well, because each
company has its own database file rather than sharing tables. What is missing
is the business around it: somebody signing up without you, a subscription
instead of a licence key, and the operations to keep other people's books
running. Until those exist, run this for one organisation, which may be yours
or may be one customer on their own server.


Before you start
----------------

  * A server running Ubuntu or Debian, with a public IP address.
  * A subdomain — books.tavonetworks.tech, say — with an A record already
    pointing at that IP. Do this first; certbot cannot get a certificate for
    a name that does not resolve to the machine asking for it.
  * Root access.


Installing
----------

    scp -r NexoraBooks-2.11.9 you@yourserver:~/
    ssh you@yourserver
    cd NexoraBooks-2.11.9
    sudo bash deploy/install-on-server.sh books.tavonetworks.tech
    sudo certbot --nginx -d books.tavonetworks.tech

That is the whole install. Then open the address in a browser.


FOUR THINGS THAT MATTER MORE THAN THE INSTALL
=============================================

1. CHANGE THE ADMIN PASSWORD, NOW
---------------------------------
It starts as admin / admin123, and that is now on the public internet where
anybody can try it. Change it before you enter a single figure, and turn on
two-factor sign-in for every administrator. Settings > Users.

Everything else on this list can wait an hour. This cannot.


2. THE LICENCE IS TIED TO THIS SERVER
-------------------------------------
Nexora Books locks a licence to one machine. On a server that machine is the
server, so the machine code shown under Settings > Licence is the server's,
not yours. Issue a licence against it from your own computer:

    python issue_licence.py

Without one it runs for 30 days and then stops accepting new entries — every
screen still opens and every report still prints, but nobody can post. Do this
in week one, not week five.

Note also that rebuilding the server from scratch gives it a new machine code,
and therefore needs a new licence. That is free; it is just a thing to know
before it happens on a Friday.


3. BACKUPS ARE YOURS NOW, NOT THE CUSTOMER'S
--------------------------------------------
On Windows the books sat on a desk and somebody could copy them to a flash
drive. Here they are in /var/lib/nexorabooks on a machine nobody looks at.

Turn on the automatic backup inside the software (Settings > Backup), and then
copy that folder OFF this server every night — to object storage, another
machine, anywhere that is not this one. A backup that lives on the server it
protects is not a backup.

    tar czf /tmp/nexora-$(date +%F).tar.gz -C /var/lib nexorabooks

Then restore from one, onto a spare machine, before you need to. A backup
nobody has ever restored is a guess.


4. IT IS ON THE OPEN INTERNET NOW
---------------------------------
This software was written for one office network where everybody was in the
same building. That assumption is gone, and a few things follow from it:

  * Two-factor sign-in for everybody, not just administrators. Settings >
    Users. It costs each person ten seconds a day.
  * Give every person their own account. A shared login makes the audit trail
    worthless, and the audit trail is the reason to have accounting software
    rather than a spreadsheet.
  * Keep the server patched:  sudo apt-get update && sudo apt-get upgrade
  * Consider fail2ban. Nginx rate-limits the sign-in page and the software
    throttles wrong passwords, but a third layer costs nothing.
  * The seller/ folder holds the key that signs licences. It is NOT on this
    server and must never be put on it — the install script refuses to copy
    seller/ even when it is sitting in the folder you run it from. Anybody who
    takes that key can issue licences in your name, for free, forever.


Running it day to day
---------------------

    sudo systemctl status nexorabooks      is it running
    sudo systemctl restart nexorabooks     restart it
    sudo journalctl -u nexorabooks -f      watch what it is doing
    sudo journalctl -u nexorabooks -n 50   the last fifty lines

Updating to a newer version:

    scp -r NexoraBooks-<new> you@yourserver:~/
    ssh you@yourserver
    cd NexoraBooks-<new>
    sudo bash deploy/install-on-server.sh books.tavonetworks.tech

It replaces the application and leaves /var/lib/nexorabooks untouched. Take a
backup first anyway.


What is different from the Windows version
------------------------------------------

  * No installer, no signing certificate, no SmartScreen warning.
  * No VPN. Staff anywhere with a browser and a password.
  * The certificate is a real one from Let's Encrypt, renewed automatically,
    so there is no "your connection is not private" warning to click through.
  * Updates happen once, here, rather than on every staff member's machine.
  * If this server is down, nobody can work. That is the trade you have made,
    and it is why the backup section above is not optional.


                                        Tavo Networks Limited (RC 8237044)
                                        support@tavonetworks.tech
