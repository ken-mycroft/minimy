from threading import Event
#from sva_base import SimpleVoiceAssistant
from bus.Message import Message
from bus.MsgBusClient import MsgBusClient
import time, os
from subprocess import Popen, PIPE, STDOUT
from framework.util.utils import CommandExecutor, LOG, Config, MediaSession
from framework.message_types import MSG_MEDIA, MSG_SKILL

class SVAMediaPlayerSkill:
    """
    the media player plays wav and mp3 files. it has several
    interesting features. first it can pause an active media
    session and play a new one. it stacks these in the 
    paused_sessions q. the media player does not have a paused
    state as such. 
    """
    def __init__(self, bus=None, timeout=5):
        self.skill_id = 'media_player_service'
        if bus is None:
            bus = MsgBusClient(self.skill_id)
        self.bus = bus

        base_dir = os.getenv('SVA_BASE_DIR')
        log_filename = base_dir + '/logs/media_player.log'
        self.log = LOG(log_filename).log
        self.log.info("Starting SVA MediaPlayer Service")

        self.is_running = False
        self.next_session_id = 0
        self.current_session = MediaSession(0, None)
        self.paused_sessions = []

        # states = idle, playing or paused
        self.state = 'idle'

        self.bus.on(MSG_MEDIA, self.handle_message)


    def send_message(self, target, message):
        # send a standard skill message on the bus.
        # message must be a dict
        message['from_skill_id'] = self.skill_id
        self.bus.send(MSG_SKILL, target, message)


    def pause(self,msg):
        self.log.info("** MediaPlayer pause hit ** state=%s, msg=%s" % (self.state, msg))
        if self.state == 'playing':
            self.current_session.correlator = msg.data.get('correlator','')
            # send signal to run()
            self.state = 'paused'
        else:
            self.log.debug("Pause Ignored - state = %s" % (self.state,))


    def resume(self,msg):
        self.log.debug("** MediaPlayer resume hit. state = %s" % (self.state,))
        if self.state == 'paused':
            self.state = 'resumed'
            self.current_session = self.paused_sessions.pop()
            if self.current_session.ce.proc is not None:
                # if we have an active process, resume it
                if self.current_session.media_type == 'wav':
                    self.current.session.ce.send(' ')
                else:
                    self.current.session.ce.send('s')
        elif self.state == 'idle':
            if len(self.paused_sessions) > 0:
                self.log.debug("MediaPlayer was idle but I have paused sessions to restart")
                self.current_session = self.paused_sessions.pop()
                if self.current_session.ce.proc is not None:
                    # if we have an active process, resume it
                    if self.current_session.media_type == 'wav':
                        self.current_session.ce.send(' ')
                    else:
                        self.current_session.ce.send('s')
                self.state = 'resumed'
        else:
            self.log.debug("Resume Ignored - state = %s" % (self.state,))


    def clear_q(self,msg):
        self.log.debug("** MediaPlayer:clear_q(), state is %s, current_session is %s" % (self.state, self.current_session))
        self.state = 'idle'
        local_q = self.current_session.media_queue
        for mentry in local_q:
            self.log.error("BUG! ClearQ must deal with this file:%s" % (mentry['file_uri'],))
        self.current_session.media_queue = []


    def play_file(self,msg):
        # add file to end of q if not 
        # playing change state to playing
        from_skill = msg.data['from_skill_id']
        file_uri = msg.data['file_uri']
        play_session_id = msg.data['session_id']
        self.log.info("MediaPlayer: PlaySID=%s, CurrentSID=%s, play file:%s" % (play_session_id, self.current_session.session_id, file_uri))
        if play_session_id == self.current_session.session_id:
            media_entry = {
                    'file_uri':file_uri,
                    'delete_on_complete':msg.data['delete_on_complete'],
                    'from_skill_id':from_skill
                    }
            self.current_session.media_queue.append(media_entry)

            if self.state == 'idle':
                self.state = 'playing'

        else:
            self.log.warning("Warning! Play file request from non active session ignored for now!!!!")


    def send_session_end_notify(self, reason):
        info = {
                'error':'',
                'subtype':'media_player_command_response',
                'response':'session_ended',
                'correlator':self.current_session.correlator,
                'reason':reason,
                'session_id':self.current_session.session_id,
                'skill_id':self.current_session.owner,
                'from_skill_id':'media_player_service',
                }
        tmp_target = self.current_session.owner
        self.current_session.owner = None
        self.log.info("MediaPlayer send_session_end_notify() - reason=%s, setting current_session.sid to 0, it was %s" % (reason, self.current_session.session_id,))
        self.current_session.session_id = 0
        self.current_session.media_queue = []
        return self.send_message(tmp_target, info)


    def send_session_reject(self,reason,msg):
        # send session reject message on bus")
        data = msg.data
        info = {
                'error':reason,
                'subtype':'media_player_command_response',
                'response':'session_reject',
                'correlator':self.current_session.correlator,
                'skill_id':data['from_skill_id'],
                'from_skill_id':'media_player_service',
                }
        return self.send_message(data['from_skill_id'], info)


    def send_session_paused(self, session_id, target):
        info = {
                'error':'',
                'subtype':'media_player_command_response',
                'response':'session_paused',
                'correlator':self.current_session.correlator,
                'session_id':session_id,
                'skill_id':target,
                'from_skill_id':'media_player_service',
                }
        return self.send_message(target, info)


    def send_session_confirm(self, msg):
        info = {
                'error':'',
                'subtype':'media_player_command_response',
                'response':'session_confirm',
                'correlator':self.current_session.correlator,
                'session_id':self.current_session.session_id,
                'skill_id':msg.data['from_skill_id'],
                'from_skill_id':'media_player_service',
                }
        return self.send_message(msg.data['from_skill_id'], info)


    def stop_session(self,msg):
        self.log.info("MediaPlayer: stop_session() state = %s, current sess id=%s, session_id to stop=%s, sessMediaType:%s" % (self.state,self.current_session.session_id,msg.data['session_id'], self.current_session.media_type))

        data = msg.data
        if data['from_skill_id'] != self.current_session.owner:
            sid = msg.data['session_id']
            target = data['from_skill_id']
            self.log.warning("StopSession Ingored owner:%s, requester:%s, sending cancelled confirmation anyway for msid:%s." % (self.current_session.owner, target, sid))
            info = {
                    'error':'',
                    'subtype':'media_player_command_response',
                    'response':'session_ended',
                    'correlator':sid,
                    'reason':'killed',
                    'session_id':sid,
                    'skill_id':target,
                    'from_skill_id':'media_player_service',
                    }
            return self.send_message(target, info)

        else:
            if self.current_session.ce.proc is not None:
                if self.current_session.media_type == 'wav':
                    try:
                        self.current_session.ce.kill()
                    except:
                        self.log.warning("Exception in media player killing wav play")
                else:
                    try:
                        self.current_session.ce.kill()
                    except:
                        self.log.warning("Exception in media player killing mp3 play")
                    # this should work better but it doesn't
                    #self.current_session.ce.send('s')
            else:
                self.log.debug("MediaPlayer:stop_session(): no currently executing command!")
                pass

            self.send_session_end_notify('killed')
            self.current_session.owner = None
            self.log.info("MediaPlayer stop_session(), setting current_session.sid to 0, it was %s" % (self.current_session.session_id,))
            self.current_session.session_id = 0
            self.current_session.time_out_ctr = 0
            return self.clear_q(msg)


    def start_session(self,msg):
        self.log.debug("MediaPlayer:start_session() curr sid = %s, msg=%s" % (self.current_session.session_id, msg))
        data = msg.data
        from_skill_id = data['from_skill_id']
        correlator = data.get('correlator','')

        if from_skill_id is None or from_skill_id == '':
            self.log.warning("MediaPlayer:start_session(): Can't start session for unknown source! %s" % (data,))
            reason = 'session_unknown_source'
            return self.send_session_reject(reason, msg)

        if self.current_session.owner is None:
            self.current_session.owner = from_skill_id
            self.next_session_id += 1
            self.current_session.session_id = self.next_session_id
            self.current_session_time_out_ctr = 0
            self.current_session.correlator = correlator
            return self.send_session_confirm(msg)

        reason = 'session_busy'
        if self.current_session.owner == from_skill_id:
            # its owner doing the requesting
            self.next_session_id += 1
            self.current_session.session_id = self.next_session_id
            self.current_session_time_out_ctr = 0
            self.current_session.correlator = correlator
            return self.send_session_confirm(msg)

        self.log.info("MediaPlayer: is busy, owner is %s, requester is %s" % (self.current_session.owner,from_skill_id))
        return self.send_session_reject(reason, msg)


    def handle_message(self,msg):
        data = msg.data
        if data['subtype'] == 'media_player_command':
            command = data['command']
            if command == 'play_media':
                return self.play_file(msg)
            elif command == 'pause_session':
                return self.pause(msg)
            elif command == 'resume_session':
                return self.resume(msg)
            elif command == 'clear_q':
                return self.clear_q(msg)
            elif command == 'start_session':
                return self.start_session(msg)
            elif command == 'stop_session':
                return self.stop_session(msg)
            else:
                self.log.debug("MediaPlayer - Unrecognized command = %s" % (command,))


    def wait_for_end_play(self, media_entry):
        while not self.current_session.ce.is_completed() and self.state == 'playing':
            time.sleep(0.01)

        self.log.debug("wait_for_end() state is %s, owner=%s, file=%s" % (self.state,self.current_session.owner,media_entry['file_uri']))

        if self.state == 'paused':
            self.log.info("MediaPlayer Pausing current session")
            if self.current_session.ce.proc is not None:
                # if we have an active process, pause it
                if self.current_session.media_type == 'wav':
                    self.current_session.ce.send(' ')
                    self.log.info("MediaPlayer Paused WAV playback")
                else:
                    self.log.info("MediaPlayer Paused MP3 playback")
                    self.current_session.ce.send('s')
                time.sleep(0.001)  # give ce a chance

            # push media entry onto paused stack
            # emulate deepcopy because of stupid thread.lock on process in cs
            ms_copy = MediaSession(self.current_session.session_id, self.current_session.owner)
            ms_copy.correlator = self.current_session.correlator
            ms_copy.media_queue = self.current_session.media_queue
            ms_copy.ce = self.current_session.ce
            ms_copy.time_out_ctr = self.current_session.time_out_ctr
            ms_copy.media_type = self.current_session.media_type
            self.paused_sessions.append( ms_copy )
            self.send_session_paused(self.current_session.session_id, self.current_session.owner)

        elif self.state == 'resumed':
            self.log.warning("MediaPlayer ILLEGAL STATE TRANSITION playing to resumed!")

        else:
            process_exit_code = self.current_session.ce.get_return_code()
            self.log.debug("MediaPlayer end of play detected, process exit code:%s, state:%s" % (process_exit_code,self.state))
            if process_exit_code != -9:
                # remove from q if not killed
                self.current_session.media_queue = self.current_session.media_queue[1:]

                # remove from file system if requested
                if media_entry['delete_on_complete'] == 'true':
                    cmd = "rm %s" % (media_entry['file_uri'],)
                    os.system(cmd)

                if len(self.current_session.media_queue) == 0:
                    self.state = 'idle'
                    #print("MediaPlayer going idle because process ended and no more files to play for this session")
                    self.send_session_end_notify('eof')
            else:
                self.log.info("MediaPlayer process was killed (-9)")
                if media_entry['delete_on_complete'] == 'true':
                    cmd = "rm %s" % (media_entry['file_uri'],)
                    os.system(cmd)


    def run(self):
        self.log.info("MediaPlayer Running %s" % (self.is_running,))

        while self.is_running:

            if self.state == 'playing':
                # get next file to play for the current media session
                if self.current_session is None:
                    # no current session
                    break

                if len(self.current_session.media_queue) == 0:
                    # no current file to play
                    self.current_session_time_out_ctr += 1
                    break

                media_entry = self.current_session.media_queue[0]
                file_uri = media_entry['file_uri']

                fa = file_uri.split(".")
                file_ext = fa[len(fa) - 1]

                cfg = Config()
                device_id = cfg.get_cfg_val('Advanced.OutputDeviceName')
                # we currently support wav and mp3
                cmd = "mpg123 %s" % (file_uri,)
                if device_id is not None:
                    cmd = "mpg123 -a " + device_id + " " + file_uri

                self.current_session.media_type = 'mp3'
                if file_ext == "wav":
                    cmd = "aplay " + file_uri
                    if device_id is not None and device_id != '':
                        cmd = "aplay -D" + device_id + " " + file_uri
                    self.current_session.media_type = 'wav'

                self.current_session.ce = CommandExecutor(cmd)

                self.wait_for_end_play(media_entry)

            if self.state == 'resumed':
                self.state = 'playing'
                self.wait_for_end_play(media_entry)

            if self.state == 'paused':
                # clear current media entry
                self.current_session.owner = None
                self.current_session.ce = None
                self.log.info("MediaPlayer run() - setting current_session.sid to 0, it was %s" % (self.current_session.session_id,))
                self.current_session.session_id = 0
                self.current_session.time_out_ctr = 0
                self.current_session.media_queue = []
                self.current_session.state = 'idle'
                self.log.debug("MediaPlayer - Paused Signal causing me to transition to idle!")
                self.state = 'idle'

            time.sleep(0.01)


if __name__ == '__main__':
    sva_mps = SVAMediaPlayerSkill()
    sva_mps.is_running = True
    sva_mps.run()
    Event().wait()  # Wait forever
