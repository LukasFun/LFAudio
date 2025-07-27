# Copyright (c) 2024 Lukas Freudenberg
# 
# Permission is hereby granted, free of charge, to any person 
# obtaining a copy of this software and associated documentation 
# files (the "Software"), to deal in the Software without 
# restriction, including without limitation the rights to use, copy,
# modify, merge, publish, distribute, sublicense, and/or sell 
# copies of the Software, and to permit persons to whom the Software 
# is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be 
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, 
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES 
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND 
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT 
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, 
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING 
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR 
# OTHER DEALINGS IN THE SOFTWARE.

# 07.05.2024, version 0.2.19
# This is a library for asynchronous audio playback using output streams.
# It uses the modules "sounddevice" by Matthias Geier, "soundfile" by Bastian Brechtold, "samplerate" by Robin Scheibler and "LFLib" by Lukas Freudenberg.

import sounddevice as sd
import soundfile as sf
import samplerate as sr
import time
#import multiprocessing
import threading
import random
import psutil
import more_itertools
import numpy as np
from LFLib import LFLib as L

# Class for an audio player
class Player:
	# Constructor method
	def __init__(self, source=None, storage=None):
		# Current position in the track queue
		self.queuePos = None
		# Indicates whether the player is busy loading a track
		self.loading = False
		# Paths of the source files
		self.sources = []
		# Number of loops remaining for each audio track (0 = infinite)
		self.loops = []
		# Raw audio data of the queued audio tracks
		self.data = []
		# Number of bytes of system memory to keep available
		self.ramBuffer = 16777216
		# Samplingrate of the stream, any audio that doesn't have this rate will be resampled to match it
		self.samplingrate = 44100
		# Marker for if the player is playing
		self.playing = False
		# Marker for if the player has reached the end of the track
		self.endTrack = False
		# Marker for if the player has reached the end of the queue
		self.endQueue = False
		# Number of initial loops for the entire queue (0 = infinite)
		self.queueLoops = 1
		# Number of loops remaining for the entire queue (0 = infinite)
		self.queueLoopsRemaining = 1
		# Current position in the raw audio data
		self.callPos = 0
		# Volume control as absolute factor
		self.volume = 1
		# Target volume to reach for volume changes that aren't instant
		self.targetVolume = None
		# Change in volume per settings-interval (in dB)
		self.volumeChange = None
		# Time delay for adjusting over-time-settings
		self.settingDelay = 0.05
		# Minimum delay between two cycles of the wait function (if too low, this will result in dropped samples for the output stream and it crashing)
		self.minDelay = 0.05
		# Callback function to be executed when the queue finishes
		self.callbackQueue = None
		# Callback function to be executed when the current track finishes
		self.callbackTrack = None
		# Block size of the stream. Smaller block size makes the player more responsive, but can lead to audio underruns.
		#self.blockSize = 4096
		self.blockSize = 1028
		# Audio stream
		self.stream = sd.OutputStream(samplerate=self.samplingrate, channels=2, blocksize=self.blockSize, callback=self.writeToStream)
		self.stream.start()
		# Create the thread to adjust settings over time like volume for fade-in and fade-out effects
		self.thread = threading.Thread(target=self.adjustOverTime, args=[])
		# Internal marker for if the settings thread should end
		self.end = False
		# Start the tread
		self.thread.start()
		
		# Thread to watch the playing status (debugging)
		#self.pthread = threading.Thread(target=self.watchPlaying, args=[])
		#self.pthread.start()
	
	#def watchPlaying(self):
	#	while not self.end:
	#		status = self.playing
	#		L.pln("playing: ", status)
	#		while self.playing == status and not self.end:
	#			time.sleep(0.01)
	#		L.pln("Playing status changed!")
	
	# Callback function to write audio data to the stream
	def writeToStream(self, outdata, *args):
		#This behaves very weird. Sometimes, the value of self.playing is false, even though it should be true!
		if not self.playing:
			outdata.fill(0)
			#L.pln("playing: ", self.playing)
			return
		l = len(outdata)
		
		dataOut = [np.multiply(sample, self.volume) for sample in self.data[self.queuePos][self.callPos:self.callPos+l]]
		self.callPos += l
		# Check if the track is over
		if self.callPos >= len(self.data[self.queuePos]):
			# New position in the next track
			self.callPos = l - len(dataOut)
			# Check if this track should be played again or go next
			if self.loops[self.queuePos] > 1:
				self.loops[self.queuePos] -= 1
			elif self.loops[self.queuePos] == 1:
				# Set marker for reaching the end of the track
				self.endTrack = True
				# Check if at the end of the queue
				if self.queuePos + 1 == len(self.sources):
					self.queuePos = 0
					# Check if the queue should be played again or stop
					if self.queueLoopsRemaining > 1:
						self.queueLoopsRemaining -= 1
					elif self.queueLoopsRemaining == 1:
						# Set marker for reaching the end of the queue
						self.endQueue = True
						# Append Stream with zeros
						dataOut = np.append(dataOut, np.zeros((self.callPos, 2)), axis=0)
						outdata[:] = dataOut
						# Stop the player
						self.stop()
						# Reset remaining queue loops to initial value
						self.queueLoopsRemaining = self.queueLoops
						return
				else:
					self.queuePos += 1
				L.pln("next track")
			# Append output with audio from the next track (if applicable)
			dataOut = np.append(dataOut, [sample * self.volume for sample in self.data[self.queuePos][:self.callPos]], axis=0)
		outdata[:] = dataOut
	
	# Query the name of the current track.
	# 
	# @return The name of the track as derived from the file name.
	def currentTrack(self):
		return L.fileNameFromPath(self.sources[self.queuePos])
	
	# Wait for the player to reach the end of the track, including all its loops.
	# If a callback function is set, it is executed at the end.
	# 
	# @param args List of arguments for the callback function.
	# @param output Whether to print status reports.
	def waitForTrack(self, args=[], output=True):
		if not self.playing and output:
			L.pln("The player isn't playing. Waiting for the track to finish may be futile.")
		self.endTrack = False
		if output:
			L.pln("Waiting for track to finish...")
		while not self.endTrack and not self.end:
			time.sleep(self.minDelay)
		if output:
			L.pln("Reached the end of the track.")
		# Only execute the callback, if it was set and the player hasn't been terminated.
		if not self.callbackTrack == None and not self.end:
			self.callbackTrack(*args)
			self.callbackTrack = None
	
	# Wait for the player to reach the end of the queue, including all its loops.
	# If a callback function is set, it is executed at the end.
	# 
	# @param args List of arguments for the callback function.
	# @param output Whether to print status reports.
	def waitForQueue(self, args=[], output=True):
		if not self.playing and output:
			L.pln("The player isn't playing. Waiting for the queue to finish may be futile.")
		self.endQueue = False
		if output:
			L.pln("Waiting for queue to finish...")
		while not self.endQueue and not self.end:
			time.sleep(self.minDelay)
		if output:
			L.pln("Reached the end of the queue.")
		# Only execute the callback, if it was set and the player hasn't been terminated.
		if not self.callbackQueue == None and not self.end:
			self.callbackQueue(*args)
			self.callbackQueue = None
	
	# Executes the callback function once the current track has finished playing.
	# 
	# @param callback Function to be executed once a track finishes. Must not have positional arguments.
	# @param args Arguments for the callback function. Must be a list.
	def setCallbackTrack(self, callback, args=[]):
		if not isinstance(args, list):
			L.pln("Arguments for callback function must be a list.")
			return
		self.callbackTrack=callback
		self.callbackTrackArgs=args
		thread = threading.Thread(target=self.waitForTrack, args=[args, False])
		# Start the tread
		thread.start()
	
	# Executes the callback function once the queue has finished playing.
	# 
	# @param callback Function to be executed once a track finishes. Must not have positional arguments.
	# @param args Arguments for the callback function. Must be a list.
	def setCallbackQueue(self, callback, args=[]):
		if not isinstance(args, list):
			L.pln("Arguments for callback function must be a list.")
			return
		self.callbackQueue=callback
		self.callbackQueueArgs=args
		thread = threading.Thread(target=self.waitForQueue, args=[args, False])
		# Start the tread
		thread.start()
	
	# Terminates the stream and Player thread
	def terminate(self):
		self.stream.abort()
		self.end = True
	
	# Add an audio track to the queue in the main thread
	# 
	# @param source path of the audio track
	# @return True if loading the audio was successful, False otherwise
	def queueSingle(self, source):
		# Check if source is a string
		if not isinstance(source, str):
			L.pln("The path for the source file must be a string.")
			return False
		try:
			L.pln("Loading \"", source, "\"...")
			# Wait to complete pending loading
			if self.loading:
				L.pln("Waiting to complete pending loading...")
			while self.loading and not self.end:
				time.sleep(0.1)
			self.loading = True
			data = []
			# Open the sound file
			f = sf.SoundFile(source)
			# Check file size
			if f.frames * 4 * 8 > psutil.virtual_memory().available - self.ramBuffer:
				L.pln("The file is too large for the system memory.")
				self.loading = False
				return False
			# Read the file in blocks and add them to the audio data
			bs = 65536
			#L.pln("Creating blocks")
			blocks = sf.blocks(source, blocksize=bs, dtype="float32")
			#L.pln("Writing blocks to internal buffer")
			# Dividing the file into blocks doesn't actually give a benefit - let's remove that
			for block in blocks:
				# If the file is mono, convert to stereo (soundfile should be able to do this, but as of version 0.12.1 this doesn't work properly)
				if f.channels == 1:
					for sample in block:
						data.append([sample, sample])
				else:
					for sample in block:
						data.append(sample)
			# Possibly resample data
			ratio = self.samplingrate / f.samplerate
			if not ratio == 1:
				L.pln("\"", source, "\" has a non-standard sampling rate. It will be resampled.")
				data = sr.resample(data, ratio, "sinc_best")
				L.pln("\"", source, "\" successfully resampled.")
			if len(self.sources) == 0:
				self.queuePos = 0
			self.sources.append(source)
			self.loops.append(1)
			self.data.append(data)
			L.pln("\"", source, "\" successfully loaded.")
			self.loading = False
		except sf.LibsndfileError:
			L.pln("Selected source file: \"", source, "\" is not an existing audio file.")
			return False
		finally:
			self.loading = False
		return True
	
	# Add an audio track to the queue.
	# 
	# @param source Path of the audio track.
	# @param threaded Whether to load the audio in its own thread, False by default.
	# @return Only if not threaded: Whether loading the track was successful.
	def queue(self, source, threaded=False):
		if not threaded:
			return self.queueSingle(source)
		else:
			t = threading.Thread(target=self.queueSingle, args=[source])
			t.start()
	
	# Wait to finish loading tracks
	def waitForLoading(self):
		while self.loading and not self.end:
			time.sleep(0.1)	
	
	# Remove an audio track from the queue (current element by default)
	# 
	# @param position index of track to remove
	def dequeue(self, position="current"):
		if len(self.sources) == 0:
			L.pln("The queue is already empty.")
			return
		if position == "current":
			position = self.queuePos.value
		if not isinstance(position, int):
			L.pln("Argument \"position\" must be an integer.")
			return
		if position < 0 or position >= len(self.sources):
			L.pln("Argument \"position\" must be non-negative and less than the number of queued audio tracks.")
			return
		# Remove the track and its data from all lists
		self.sources.pop(position)
		self.loops.pop(position)
		self.data.pop(position)
		#goNext = self.queuePos.value == position
		goNext = self.queuePos == position
		# If the removed track's position is prior to the current one, go back
		#if self.queuePos.value >= position:
		if self.queuePos >= position:
			#self.queuePos.set(self.queuePos.value - 1)
			self.queuePos -= 1
		if len(self.sources) == 0:
			self.stop()
			#self.queuePos.set(None)
			self.queuePos = None
		# If the current track is removed while playing, go to the next
		if goNext:
			self.nextTrack()
	
	# Remove the a given number of instances of an audio track from the queue
	# 
	# @param track path of the source file
	# @param count number of instances to remove (1 by default)
	def dequeueByName(self, track, count=1):
		pass
	
	# Jump to the next track
	def nextTrack(self):
		if len(self.sources) == 0:
			L.pln("You must add an audio track to the queue first.")
			return
		self.callPos = 0
		# If at the end of the queue and looping turned off, stop playing
		#if self.queuePos.value == len(self.sources) - 1 and not self.queueLoopsRemaining:
		if self.queuePos == len(self.sources) - 1 and not self.queueLoopsRemaining == 0:
			# Stop the player
			self.stop()
			# Reset remaining queue loops to initial value
			self.queueLoopsRemaining = self.queueLoops
		#self.queuePos.set((self.queuePos.value + 1) % len(self.sources))
		self.queuePos = (self.queuePos + 1) % len(self.sources)
		L.pln("next track")
	
	# Change the looping for one track
	# 
	# @param track the track to modify the looping for, current track by default
	# @param loops number of loops, 0 means infinite, 0 by default
	def loopTrack(self, track=None, loops=0):
		if not isinstance(loops, int):
			L.pln("Argument \"loop\" must be an integer.")
			return
		if track == None:
			track = self.queuePos
		self.loops[track] = loops
	
	# Change the looping for the entire queue
	# 
	# @param loop 
	def loopQueue(self, loops=0):
		if not isinstance(loops, int):
			L.pln("Argument \"loop\" must be an integer.")
			return
		self.queueLoops = loops
		self.queueLoopsRemaining = loops
	
	# Start playing
	def play(self):
		if len(self.sources) == 0:
			L.pln("You must add an audio track to the queue first.")
			return
		self.playing = True
		#L.pln("raw audio:")
		#L.pln(self.data[0])
	
	# Pause playback
	def pause(self):
		self.playing = False
	
	# Toggle playback
	def playPause(self):
		if self.playing:
			self.pause()
		else:
			self.play()
	
	# Stop playback
	def stop(self):
		self.playing = False
		self.callPos = 0
	
	# Set the volume (absolute)
	# 
	# @param volume new volume (absolute)
	# @param pace time interval over which the change occurs, instant by default
	def setVolume(self, volume, pace=0):
		target = min(max(volume, 0), 1)
		if pace == 0:
			self.volume = target
		else:
			L.pln("gradual volume change initiated")
			delta = 20 * np.log10(target / self.volume)
			self.volumeChange = self.settingDelay * delta / pace
			self.targetVolume = target
	
	# Set the volume (dB)
	# 
	# @param volume new volume (dB, 0dB equals an absolute volume of 4)
	# @param pace time interval over which the change occurs, instant by default
	def setVolumeDB(self, volume, pace=0):
		target = pow(10, volume / 20)
		self.setVolume(target, pace)	
	
	# Change the volume (by absolute amount)
	# 
	# @param delta absolute change in volume
	# @param pace time interval over which the change occurs, instant by default
	def changeVolume(self, delta, pace=0):
		target = self.volume + delta
		self.setVolume(target, pace)
	
	# Change the volume by given dB
	# 
	# @param delta dB change in volume
	# @param pace time interval over which the change occurs, instant by default
	def changeVolumeDB(self, delta, pace=0):
		target = self.volume * pow(10, delta / 20)
		self.setVolume(target, pace)
	
	# Adjusts the volume to reach a new target over a specific period of time
	# 
	# @param target 
	#def fadeVolumeAbsolute(target, )
	
	# Threaded function that handles effects like fade-in and fade-out
	def adjustOverTime(self):
		while not self.end:
			# Check if volume should be adjusted
			if not self.targetVolume == None:
				# Check if target has been reached
				if (self.volumeChange > 0 and self.volume < self.targetVolume) or (self.volumeChange < 0 and self.volume > self.targetVolume):
					# Adjust volume
					self.changeVolumeDB(self.volumeChange)
				else:
					self.targetVolume = None
			time.sleep(self.settingDelay)
	
	# Jump to a given time in an audio track
	# 
	# @param timepos timecode to jump to. Can be given as a number of seconds or in the format mm:ss or in the format h:mm:ss
	def jumpTo(self, timepos):
		pos = None
		# Convert time to an index in the audio data
		if isinstance(timepos, int):
			pos = timepos * self.samplingrate
		elif isinstance(timepos, str):
			pos = L.timeToSeconds(timepos) * self.samplingrate
		else:
			L.pln("Given position: ", timepos, " is not in a valid format.")
			return
		# Check if time is outside of the tracks bounds
		if pos >= len(self.data[self.queuePos.value]) or pos < 0:
			L.pln("Given position: ", timepos, " is outside of the current tracks bounds.")
			return
		# Jump to the new position
		self.callPos = pos
	
	# Shuffles the queue.
	# 
	# @param keepPos whether the current track will remain
	def shuffle(self, keepPos=True):
		order = list(range(len(self.sources)))
		if keepPos:
			# Remove current position
			order.pop(self.queuePos.value)
			# Shuffle the order
			random.shuffle(order)
			# Add current position back
			order.insert(self.queuePos.value, self.queuePos.value)
		else:
			# Shuffle the order
			random.shuffle(order)
		# Apply the new order to all lists
		L.reorder(self.sources, order)
		L.reorder(self.loops, order)
		L.reorder(self.data, order)
#if __name__ == '__main__':
#	pass
#	p = Player()
