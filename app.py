#!venv/bin/python2.7
import traceback
from Queue import Queue
import time
import threading
from telnetlib import Telnet, IAC, DO, WILL, WONT, SB, SE
from flask import Flask, jsonify, render_template, request, redirect, url_for, flash, g
from flask.ext.socketio import SocketIO, emit
from flask.ext.sqlalchemy import SQLAlchemy
from flask.ext.bcrypt import Bcrypt
from flask.ext.login import LoginManager, login_user, logout_user, \
        current_user, login_required
from flask_wtf import Form
from wtforms import TextField, PasswordField
from wtforms.validators import DataRequired, Length, Email, EqualTo
import re


app = Flask(__name__)
app.secret_key = 'A0Zr98j/3yX R~XHH!jmN]LWX/,?RT'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
from models import *
#app.config['SERVER_NAME'] = 'vps.rooflez.com'
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'


socketio = SocketIO(app)

telnets = {}


class TelnetConn:
    ttypes = ['ArcWeb', 'ANSI']
    MSDP_VARS = [
        'CHARACTER_NAME',
        'HEALTH', 'HEALTH_MAX',
        'MANA', 'MANA_MAX',
        'MOVEMENT', 'MOVEMENT_MAX',
        'EXPERIENCE_TNL', 'EXPERIENCE_MAX',
        'OPPONENT_HEALTH', 'OPPONENT_HEALTH_MAX',
        'OPPONENT_NAME',
        'STR', 'STR_PERM',
        'CON', 'CON_PERM',
        'VIT', 'VIT_PERM',
        'AGI', 'AGI_PERM',
        'DEX', 'DEX_PERM',
        'INT', 'INT_PERM',
        'WIS', 'WIS_PERM',
        'DIS', 'DIS_PERM',
        'CHA', 'CHA_PERM',
        'LUC', 'LUC_PERM',
        'ROOM_NAME', 'ROOM_EXITS',
        'AFFECTS',
        'LUA_EDIT']

    def __init__(self, room_id, player_ip):
        self.telnet = None
        self.read_thread = None
        self.write_thread = None

        self.room_id = room_id
        self.player_ip = player_ip
        self.write_lock = threading.Lock()
        self.ttype_index = 0
        self.write_queue = Queue()

        self.abort = False

    def start(self):
        self.read_thread = threading.Thread(target=self._listen)
        self.read_thread.daemon = True
        self.read_thread.start()

        self.write_thread = threading.Thread(target=self._write)
        self.write_thread.daemon=True
        self.write_thread.start()

    def stop(self):
        self.abort = True
        self.telnet.close()
        
    def _write(self):
        while True:
            if self.abort:
                return

            cmd = self.write_queue.get(True, None)
            with self.write_lock:
                self.telnet.write(str(cmd))
                socketio.emit('telnet_error', {},
                              room=self.room_id,
                              namespace='telnet')

    def write(self, cmd):
        self.write_queue.put(cmd)

    def sock_write(self, cmd):
        with self.write_lock:
            self.telnet.get_socket().sendall(cmd)

    def handle_msdp(self, msdp):
        env = {'ind': 0}

        def get_msdp_table():
            env['ind'] += 1
            rtn = {}
            while msdp[env['ind']] != MSDP.TABLE_CLOSE:
                kv = get_msdp_var()
                rtn[kv[0]] = kv[1]
            
            return rtn 

        def get_msdp_array():
            env['ind'] += 1
            rtn = []
            while msdp[env['ind']] != MSDP.ARRAY_CLOSE:
                v = get_msdp_val()
                rtn.append(v)

            return rtn

        def get_msdp_val():
            env['ind'] += 1

            if env['ind'] >= len(msdp):
                return ''

            if msdp[env['ind']] == MSDP.ARRAY_OPEN:
                return get_msdp_array()
            elif msdp[env['ind']] == MSDP.TABLE_OPEN:
                return get_msdp_table()
                
            # normal var
            start_ind = env['ind'] 
            while env['ind'] < len(msdp) and msdp[env['ind']] not in (MSDP.VAR, MSDP.VAL, MSDP.ARRAY_CLOSE, MSDP.TABLE_CLOSE):
                env['ind'] += 1 
            
            return msdp[start_ind:env['ind']]

        def get_msdp_var():
            env['ind'] += 1
            start_ind = env['ind']

            while msdp[env['ind']] != MSDP.VAL:
                env['ind'] += 1 

            return msdp[start_ind:env['ind']], get_msdp_val()

        result = get_msdp_var()
        socketio.emit('msdp_var', {'var': result[0], 'val': result[1]},
                      room=self.room_id,
                      namespace='/telnet')

    def write_msdp_var(self, var, val):
        # print("write_msdp_var")
        seq = IAC + SB + TelnetOpts.MSDP + MSDP.VAR + str(var) + MSDP.VAL + str(val) + IAC + SE
        # print("Seq:",seq)
        self.sock_write(seq)
        # print 'Write: ' + str(seq)

    def _negotiate(self, socket, command, option):
        # print 'Got ',ord(command),ord(option)
        # tmp = 'Got {} {}'.format(
        #     TelnetCmdLookup.get(command, ord(command)),
        #     TelnetOptLookup.get(option, ord(option))
        # )
        # print tmp
        if command == WILL:
            if option == TelnetOpts.ECHO:
                socketio.emit('server_echo', {'data': True}, room=self.room_id, namespace="/telnet")
            elif option == TelnetOpts.MSDP:
                self.sock_write(IAC + DO + TelnetOpts.MSDP)
                
                self.write_msdp_var('CLIENT_ID', "ArcWeb")
                for var_name in self.MSDP_VARS:
                    self.write_msdp_var("REPORT", var_name)
        elif command == WONT:
            if option == TelnetOpts.ECHO:
                socketio.emit('server_echo', {'data': False}, room=self.room_id, namespace="/telnet")
        elif command == DO:
            if option == TelnetOpts.TTYPE:
                self.sock_write(IAC + WILL + TelnetOpts.TTYPE)
            elif option == TelnetOpts.MXP:
                self.sock_write(IAC + WILL + TelnetOpts.MXP)
        elif command == SE:
            d = self.telnet.read_sb_data()
            # print 'got',[ord(x) for x in d]
            # tmp = 'SB got {} {}'.format(
            #     TelnetOptLookup.get(d[0], ord(d[0])),
            #     str([ord(x) for x in d[1:]])
            # )
            # print tmp
            
            if d == TelnetOpts.TTYPE + TelnetSubNeg.SEND:
                if self.ttype_index >= len(self.ttypes):  # We already sent them all, so send the player IP
                    ttype = self.player_ip
                else:
                    ttype = self.ttypes[self.ttype_index]
                    self.ttype_index += 1
                self.sock_write(IAC + SB + TelnetOpts.TTYPE + TelnetSubNeg.IS + ttype + IAC + SE)
            elif d[0] == TelnetOpts.MSDP:
                # print 'get the msdp'
                self.handle_msdp(d[1:])

    def _listen(self):
        self.ttype_index = 0
        # self.telnet = Telnet('aarchonmud.com', 7000)
        self.telnet = Telnet('rooflez.com', 7101)

        self.telnet.set_option_negotiation_callback(self._negotiate)

        socketio.emit('telnet_connect', {},
                      room=self.room_id,
                      namespace='/telnet')

        while True:
            if self.abort:
                socketio.emit('telnet_disconnect', {},
                              room=self.room_id,
                              namespace='/telnet')
                return
            d = None
            try:
                d = self.telnet.read_very_eager()
            except EOFError:
                socketio.emit('telnet_disconnect', {},
                              room=self.room_id,
                              namespace='/telnet')
                return

            except Exception as ex:
                with app.test_request_context('/telnet'):
                    socketio.emit('telnet_error', {'message': ex.message},
                                  room=self.room_id,
                                  namespace='/telnet')
                return

            if d is not None and d != '':
                socketio.emit('telnet_data', {'data': d},
                              room=self.room_id,
                              namespace='/telnet' )
            time.sleep(0.050)


@socketio.on('ack_rx', namespace='/telnet')
def ws_ack_rx(message):
    # print("ack_rx running")
    tn = telnets.get(request.sid, None)
    if tn is None:
        print("OMG")
        return

    # print "Message ",  message
    try:
        tn.write_msdp_var("ACK_RX", message)
    except Exception as e:
        print "help"
        print e.message
        print traceback.print_exc()

    # print "FINISSIMO"


@socketio.on('save_lua', namespace='/telnet')
def ws_save_lua(message):
    print("save_lua running")
    tn = telnets.get(request.sid, None)
    if tn is None:
        return

    tn.write_msdp_var("LUA_EDIT", message['data']) 


@socketio.on('request_test_socket_response', namespace='/telnet')
def asdf(message):
    emit('reply_test_socket_response', {}, namespace='/telnet')


@socketio.on('connect', namespace='/telnet')
def ws_connect():
    print('connect running')
    emit('ws_connect', {}, namespace="/telnet")


@socketio.on('disconnect', namespace='/telnet')
def ws_disconnect():
    print('disconnect running ' + request.sid)
    if request.sid in telnets:
        tn = telnets[request.sid]
        tn.stop()
        del telnets[request.sid]
    # emit('ws_disconnect', {}, namespace="/telnet")


@socketio.on('open_telnet', namespace='/telnet')
def ws_open_telnet(message):
    print 'opening telnet'
    tn = TelnetConn(request.sid, request.remote_addr)
    tn.start()
    telnets[request.sid] = tn


@socketio.on('close_telnet', namespace='/telnet')
def ws_close_telnet(message):
    print 'closing telnet'
    tn = telnets.get(request.sid, None)
    if tn is not None:
        tn.stop()
        del telnets[request.sid]


@socketio.on('send_command', namespace='/telnet')
def ws_send_command(message):
    cmd = message['data']
    if request.sid not in telnets:
        socketio.emit('telnet_error', {'data': 'Telnet connection does not exist.'},
                      room=request.sid,
                      namespace='/telnet')

    telnets[request.sid].write(cmd+"\n")


@app.route("/")
def client():
    print "client.html baby"
    return render_template('client.html')


@app.route("/resize")
@login_required
def resize():
    print "resize"
    return render_template('resize.html')


#login_manager.login_view = "users.login"


@login_manager.user_loader
def load_user(user_id):
    #User.query.filter(User.id == int(user_id)).first()
    return User.query.get(user_id)


class LoginForm(Form):
    username = TextField('Username', validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired()])


class RegisterForm(Form):
    username = TextField(
        'username',
        validators=[DataRequired(), Length(min=3, max=25)]
    )
    email = TextField(
        'email',
        validators=[DataRequired(), Email(message=None), Length(min=6, max=40)]
    )
    password = PasswordField(
        'password',
        validators=[DataRequired(), Length(min=6, max=25)]
    )
    confirm = PasswordField(
        'Repeat password',
        validators=[
            DataRequired(), EqualTo('password', message='Passwords must match.')
        ]
    )


@app.route("/login", methods=['GET', 'POST'])
def login():
    if g.user is not None and g.user.is_authenticated:
        return redirect(url_for('client'))

    error = None
    form = LoginForm(request.form)
    if request.method == 'POST':
        if form.validate_on_submit():
            user = User.query.filter_by(name=request.form['username']).first()
            if user is not None and bcrypt.check_password_hash(
                user.password, request.form['password']):
                login_user(user)
                # flash('You were logged in. Go Crazy.')
                print user
                return redirect(url_for('client'))

            else:
                error = 'Invalid username or password.'
    return render_template('login.html', form=form, error=error)


@app.route("/logout", methods=['GET'])
@login_required
def logout():
    logout_user()
    return redirect(url_for('client'))


@app.route("/register", methods=['GET', 'POST'])
def register():
    form = RegisterForm()
    if form.validate_on_submit():
        user = User(
            name=form.username.data,
            email=form.email.data,
            password=form.password.data
        )
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return redirect(url_for('client'))
    return render_template("register.html", form=form)


@app.before_request
def before_request():
    g.user = current_user


class TelnetCmds(object):
    SE = chr(240)  # End of subnegotiation parameters
    NOP = chr(241)  # No operation
    DM = chr(242)  # Data mark
    BRK = chr(243)  # Break
    IP = chr(244)  # Suspend
    AO = chr(245)  # Abort output
    AYT = chr(246)  # Are you there
    EC = chr(247)  # Erase character
    EL = chr(248)  # Erase line
    GA = chr(249)  # Go ahead
    SB = chr(250)  # Go Subnegotiation
    WILL = chr(251)  # will
    WONT = chr(252)  # wont
    DO = chr(253)  # do
    DONT = chr(254)  # dont
    IAC = chr(255)  # interpret as command
TelnetCmdLookup = {v: k for k, v in TelnetCmds.__dict__.iteritems()}


class TelnetOpts(object):
    ECHO = chr(1)
    SUPPRESS_GA = chr(3)
    STATUS = chr(5)
    TIMING_MARK = chr(6)
    TTYPE = chr(24)
    WINDOW_SIZE = chr(31)
    CHARSET = chr(42)
    MSDP = chr(69)
    MCCP = chr(70)
    MSP = chr(90)
    MXP = chr(91)
    ATCP = chr(200)
TelnetOptLookup = {v: k for k, v in TelnetOpts.__dict__.iteritems()}


class TelnetSubNeg(object):
    IS = chr(0)
    SEND = chr(1)
    ACCEPTED = chr(2)
    REJECTED = chr(3)
TelnetSubNegLookup = {v: k for k, v in TelnetSubNeg.__dict__.iteritems()}


class MSDP(object):
    VAR = chr(1)
    VAL = chr(2)
    TABLE_OPEN = chr(3)
    TABLE_CLOSE = chr(4)
    ARRAY_OPEN = chr(5)
    ARRAY_CLOSE = chr(6)
MSDPLookup = {v: k for k, v in MSDP.__dict__.iteritems()}


if __name__ == "__main__":
    # app.debug=True
    # app.run('0.0.0.0')
    # db.create_all()
    socketio.run(app, '0.0.0.0', port=5000, worker=1)
    # db.session.commit()
