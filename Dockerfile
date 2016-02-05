FROM python:2.7

RUN mkdir -p /pytrader
ADD ./requirements.txt /pytrader/requirements.txt
WORKDIR /pytrader
RUN pip install -r requirements.txt

RUN echo America/New_York | tee /etc/timezone && dpkg-reconfigure --frontend noninteractive tzdata

# ADD . /pytrader
# ENV TERM screen-256color
# ENTRYPOINT ["./pytrader.py"]
# CMD ["--strategy=balancer"]

RUN apt-get update && apt-get upgrade -y
RUN apt-get install -y supervisor openssh-server
RUN apt-get install -y screen

RUN mkdir /root/.ssh
ADD authorized_keys /root/.ssh/authorized_keys

RUN /bin/echo -e "#!/bin/bash\n\
# service ntp start\n\
sed -ri 's/UsePAM yes/#UsePAM yes/g' /etc/ssh/sshd_config && sed -ri 's/#UsePAM no/UsePAM no/g' /etc/ssh/sshd_config\n\
service ssh start\n\
exec >/dev/tty 2>/dev/tty </dev/tty\n\
cd /pytrader && screen -s /bin/bash -dmS pytrader ./pytrader.py --strategy=balancer\n\
" > /pytrader/launch-pytrader.sh
RUN chmod +x /pytrader/launch-pytrader.sh

# Setup supervisord
RUN /bin/echo -e "[supervisord]\n\
nodaemon=true\n\
\n\
[program:pytrader]\n\
directory=/pytrader\n\
user=root\n\
command=/pytrader/launch-pytrader.sh\n\
startsecs=0" > /etc/supervisor/conf.d/pytrader.conf

# Add "screen -r" to .profile
RUN /bin/echo -e "\n\
cd /pytrader\n\
screen -r\n\
" >> /root/.profile

ADD . /pytrader
EXPOSE 22
CMD ["-n", "-c", "/etc/supervisor/conf.d/pytrader.conf"]
ENTRYPOINT ["/usr/bin/supervisord"]
