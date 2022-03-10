import requests, json, time, glob, os
from framework.services.intent.nlp.shallow_parse.parse_sentence import parse_sentence
from framework.services.intent.nlp.shallow_parse.parse_question import parse_question
from framework.util.utils import LOG, Config, get_wake_words, aplay
from bus.Message import Message
from bus.MsgBusClient import MsgBusClient
from framework.message_types import (
        MSG_UTTERANCE, 
        MSG_MEDIA, 
        MSG_RAW, 
        MSG_SYSTEM
        )
class UttProc:
    """
    English language intent parser. Monitors the save_text/ FIFO 
    for utterances to process. Emits utterance messages. If skill_id
    is not '' the utterance matched an intent in the skill_id skill.
    """
    def __init__(self, bus=None, timeout=5):
        self.skill_id = 'intent_service'
        if bus is None:
            bus = MsgBusClient(self.skill_id)
        self.bus = bus
        self.intents = {}

        base_dir = os.getenv('SVA_BASE_DIR')
        self.tmp_file_path = base_dir + '/tmp/'
        log_filename = base_dir + '/logs/intent.log'
        self.log = LOG(log_filename).log
        self.log.info("Intent Service Starting")
        self.earcon_filename = base_dir + "/framework/assets/earcon_start.wav"
        self.last_tts_text_path = base_dir + "/framework/services/tts/last_tts_generated.txt"

        # set this to True before calling run()
        # set to false to discontinue running.
        self.is_running = False

        qw_file_name = 'question_words.txt'
        base_dir = os.getenv('SVA_BASE_DIR')
        if base_dir is not None:
            qw_file_name = base_dir + '/framework/' + qw_file_name

        self.question_words = []
        fh = open(qw_file_name)
        for line in fh.readlines():
            line = line.strip()
            if len(line) > 1:
                self.question_words.append(line)
        fh.close()

        cfg = Config()
        self.crappy_aec = cfg.get_cfg_val('Advanced.CrappyAEC')
        remote_nlp = cfg.get_cfg_val('Advanced.NLP.UseRemote')
        self.use_remote_nlp = True
        if remote_nlp and remote_nlp == 'n':
            self.use_remote_nlp = False

        # we try to keep in sync with the system skill
        # so we can limit OOBs to verbs which have been
        # registered
        self.recognized_verbs = []
        # TODO need get_stop_aliases() method in framework.util.utils
        self.stop_aliases = ['stop', 'terminate', 'abort', 'cancel', 'kill', 'exit']
        self.wake_words = []
        wws = get_wake_words()
        for ww in wws:
            self.wake_words.append( ww.lower() )

        self.bus.on('register_intent', self.handle_register_intent)
        self.bus.on('system', self.handle_system_message)


    def handle_system_message(self, message):
        # we try to stay in-sync with the system skill regarding OOBs
        data = message.data
        self.log.debug("Intent svc handle sys msg %s" % (data,))
        if data['skill_id'] == 'system_skill':
            # we only care about system messages - reserve and release oob
            self.log.debug("Intent service handle system message %s" % (message.data,))

            if data['subtype'] == 'reserve_oob':
                self.recognized_verbs.append( data['verb'] )

            if data['subtype'] == 'release_oob':
                del self.recognized_verbs[ data['verb'] ]


    def is_oob(self, utt):
        # we don't just match hard oobs, we also look for top
        # oobs using special handling to overcome poor hardware
        # return values:
        #  't' - normal oob detected
        #  'o' - aec oob detected
        #  'f' - no oob detected
        ua = utt.split(" ")

        if len(ua) == 1:
            #if ua[0] in self.recognized_verbs or ua[0] in self.stop_aliases:
            # we special case pause, resume and stop aliases
            if ua[0] in self.recognized_verbs or ua[0] in self.stop_aliases or ua[0] == 'pause' or ua[0] == 'resume':
                self.log.error("Intent Barge-In Normal OOB Detected")
                return 't'

        # in a system with aec you can just return 'f' here
        if not self.crappy_aec:
            return 'f'

        # here we try to deal with poor quality input (IE no AEC).
        # you can disable this on a device with good AEC. also see
        # sva_base for other code which would use this config value
        # if it were available (poor audio input quality indicator)
        for ww in self.wake_words:
            for alias in self.stop_aliases:
                oob_phrase = ww + ' ' + alias
                if oob_phrase.lower() in utt.lower() or ( alias in utt.lower() and ww in utt.lower() ):
                    self.log.warning("** Intent Barge-In Exception Detected, Don't Worry, I'm Handling It! **")
                    return 'o'

        # logical AEC for poor quality audio systems
        last_tts = ''
        if self.crappy_aec:
            try:
                fh = open(self.last_tts_text_path)
                last_tts = fh.read()
                fh.close()
            except:
                pass
            last_tts = last_tts.lower()

        utt = utt.lower()
        new_utt = set( last_tts.split(" ") ) - set( utt.split(" ") )
        new_utt = " ".join(new_utt)
        new_utt = new_utt.lower()
        for ww in self.wake_words:
            for alias in self.stop_aliases:
                oob_phrase = ww + ' ' + alias
                if oob_phrase in new_utt or ( alias in new_utt and ww in new_utt ):
                    self.log.info("***** IntentSvc Barge-In2 Exception Detected, Don't Worry, I'm Handling It! *****")
                    return 'o'

        return 'f'


    def utt_to_intent(self, utt):
        # TODO obviously this needs to be expanded
        vrb = utt.split(" ")[0]
        resp = "I"
        for wrd in self.question_words:
            if utt.startswith(wrd):
                resp = "Q"
                break
        return resp


    def scrub_sentence(self, utt):
        # really need an overt pre-processing phase
        utt = utt.replace("per cent", " percent")
        utt = utt.replace("%", " percent")
        utt = utt.replace(".", " ")
        utt = utt.replace(",", "")
        utt = utt.replace("please ", "")
        utt = utt.replace(" please", "")
        return utt


    def send_utt(self, utt):
        target = utt.get('skill_id','*')
        if target == '':
            target = '*'
        if utt == 'stop':
            target = 'system_skill'
        self.bus.send(MSG_UTTERANCE, target, {'utt': utt,'subtype':'utt'})


    def send_media(self, info):
        self.bus.send(MSG_MEDIA, 'media_skill', info)


    def send_oob_to_system(self, utt, contents):
        info = {
                'error':'', 
                'subtype':'oob', 
                'skill_id':'system_skill', 
                'from_skill_id':self.skill_id, 
                'sentence_type':'I', 
                'sentence':contents, 
                'verb':utt, 
                'intent_match':''
                }
        self.bus.send(MSG_SYSTEM, 'system_skill', info)


    def get_question_intent_match(self, info):
        # ugly but necessary?
        aplay(self.earcon_filename)
        # for qustions we cheat and use existing
        # intents and minor fuzzy matching
        skill_id = ''
        for intent in self.intents:
            stype, subject, verb = intent.split(":") 
            if stype == 'Q' and subject in info['np'] and verb == info['qword']:
                skill_id = self.intents[intent]['skill_id']
                intent_state = self.intents[intent]['state']
                return skill_id, intent

        return skill_id, ''


    def get_intent_match(self, info):
        # ugly but necessary?
        aplay(self.earcon_filename)
        # for utterances of type command
        # an intent match is a subject:verb
        skill_id = ''
        verb_or_qword = ''

        intent_type = 'Q'
        if info['sentence_type'] == 'I':
            intent_type = 'C'

        subject = info['subject']
        if subject:
            subject = subject.replace(" the","")
            subject = subject.replace("the ","")
            subject = subject.replace("an ","")
            subject = subject.replace("a ","")

        key = intent_type + ':' + subject + ':' + info['verb']
        if intent_type == 'Q':
            key = intent_type + ':' + subject + ':' + info['qtype']

        self.log.debug("get intent match key is %s" % (key,))

        if key in self.intents:
            skill_id = self.intents[key]['skill_id']
            intent_state = self.intents[key]['state']
            self.log.debug("intent matched[%s] skill=%s, intent_state=%s" % (key,skill_id, intent_state))
            return skill_id, key

        return skill_id, ''


    def handle_register_intent(self, msg):
        data = msg.data

        key = data['intent_type'] + ':' + data['subject'] + ':' + data['verb']

        if key in self.intents:
            self.log.warning("Error - intent clash! intent key=%s, skill_id=%s" % (key,data['skill_id']))
        else:
            self.intents[key] = {'skill_id':data['skill_id'], 'state':'enabled'}


    def run(self):
        self.log.info("intent processor is_running is %s" % (self.is_running,))
        while self.is_running:
            # get all text files in the input directory
            mylist = sorted( [f for f in glob.glob(self.tmp_file_path + "save_text/*.txt")] )

            # if we have at least one
            if len(mylist) > 0:
                # TODO clean this slop up

                # take the first
                txt_file = mylist[0]

                # grab contents
                fh = open(txt_file)
                contents = fh.read()
                fh.close()

                start = contents.find("]")
                utt_type = contents[1:start]
                utt = contents[start+1:]

                utt = self.scrub_sentence(utt)

                info = {
                        'error':'uninitialized', 
                        'subtype':'', 
                        'skill_id':'', 
                        'from_skill_id':'', 
                        'sentence_type':'', 
                        'sentence':'', 
                        'raw_input':contents, 
                        'intent_match':''
                        }

                # we special case OOBs here
                oob_type = self.is_oob(utt)
                if oob_type == 't':
                    res = self.send_oob_to_system(utt, contents) 
                if oob_type == 'o':
                    res = self.send_oob_to_system('stop', contents) 
                
                elif utt_type != 'RAW':
                    # question or imperative 
                    sentence_type = self.utt_to_intent(utt)

                    info['sentence_type'] = sentence_type
                    info['sentence'] = utt

                    if sentence_type == 'I':
                        info = parse_sentence(utt, self.use_remote_nlp)

                        if info['sentence_type'] == 'system':
                            # we missed but parse_sentence() picked up an oob
                            # we probably want to ignore this if it is not registered.
                            if utt in self.recognized_verbs:
                                self.send_oob_to_system(utt, contents)
                            else:
                                self.log.warning("Ignoring not recognized OOB in intent_service '%s' not found in %s" % (utt,self.recognized_verbs))
                                pass
                        else:
                            if info['error'] == '':
                                # otherwise check for intent match
                                info['skill_id'], info['intent_match'] = self.get_intent_match(info)

                    else:
                        # else probably a question
                        info = parse_question(utt, self.use_remote_nlp)
                        info['sentence_type'] = sentence_type
                        info['sentence'] = utt
                        if info['error'] == '':
                            info['skill_id'], info['intent_match'] = self.get_question_intent_match(info)

                    if info['error'] == 'media':
                        # its a media type sentence
                        info['error'] = ''
                        info['skill_id'] = 'media_skill'
                        info['from_skill_id'] = self.skill_id
                        info['subtype'] = 'media_query'
                        res = self.send_media(info) 
                    else:
                        # otherwise just a normal utterance
                        info['sentence'] = contents
                        res = self.send_utt(info) 

                else:
                    # send raw messages to the system skill
                    # and let it figure it out
                    if contents:
                        # [RAW]bla bla bla ---> bla bla bla
                        self.bus.send(MSG_RAW, 'system_skill', {'utterance': contents[5:]})

                # remove it from input directory
                os.remove(txt_file)

            time.sleep(0.125)


if __name__ == '__main__':
    up = UttProc()
    up.is_running = True
    up.run()
