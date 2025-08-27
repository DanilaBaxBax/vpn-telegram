# vpn-telegram
# Развертывание на сервере
# 1) Сохранить скрипт и выдать права
chmod +x /root/vpn_conf.sh

# 2) Установка сервера (iface/порт/подсеть/DNS можно менять флагами)
sudo /root/vpn_conf.sh install --port 51820 --subnet 10.8.0.0/24 --dns 1.1.1.1,9.9.9.9

# 3) Добавить клиента и сразу показать QR в терминале (опционально)
sudo /root/vpn_conf.sh add alice --qr

# 4) Показать конфиг/QR позже
sudo /root/vpn_conf.sh show alice --qr

# 5) Экспорт файла для выдачи через бота/почту и т.п.
sudo /root/vpn_conf.sh export alice --path /root

# Команды
./vpn_conf.sh install [--iface wg0] [--port 51820] [--subnet 10.8.0.0/24] [--dns 1.1.1.1,9.9.9.9]
./vpn_conf.sh add <username> [--ip 10.8.0.X] [--qr] [--ipv6]
./vpn_conf.sh revoke <username>
./vpn_conf.sh list
./vpn_conf.sh show <username> [--qr]
./vpn_conf.sh export <username> [--path /dir]
./vpn_conf.sh status | restart

# Флаги
--iface — имя интерфейса (по умолчанию wg0).
--port — UDP-порт сервера.
--subnet — подсеть для VPN-клиентов (сервер получает .1/24).
--dns — DNS-серверы для клиентов; в .conf попадёт первый.
--ip — зафиксировать IP клиента (иначе выдаст следующий свободный).
--qr — печатать QR в терминал (PNG всё равно сохраняется).
--ipv6 — добавить ::/0 в AllowedIPs клиента (сам сервер IPv6-адрес не настраивает).

#Директории
Конфиг интерфейса: /etc/wireguard/<iface>.conf
Ключи сервера: /etc/wireguard/keys/server-<iface>.key|pub
Клиенты: /etc/wireguard/clients/<username>/
username.conf — конфиг для импорта
qr.png — QR-код
public.key, private.key, psk.key, peer.conf — служебные файлы
DNS для клиентов: /etc/wireguard/<iface>.dns

#Обслуживание
# Проверка состояния
sudo /root/vpn_setup.sh status

# Перезапуск интерфейса wg0
sudo /root/vpn_setup.sh restart

# Список клиентов и активных peer'ов
sudo /root/vpn_setup.sh list

# Отозвать клиента
sudo /root/vpn_setup.sh revoke alice
