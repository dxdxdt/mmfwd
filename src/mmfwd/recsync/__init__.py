import datetime
from fcntl import LOCK_EX, flock
import glob
from io import SEEK_SET
import os
import subprocess
import sys
from typing import assert_never
from .exceptions import *

def acquire_plock ():
	f = open("mmfwd-recsync.pid", "w+")
	try:
		flock(f, LOCK_EX)
		f.write(str(os.getpid()))
		f.flush()

		return f
	except OSError as e:
		f.seek(0, SEEK_SET)
		pid = f.read(255).strip()
		f.close()

		raise MMFWDRSProcessLockException(
			"Process lock failed (Another process: %s)" % (pid),
			e)

	assert_never()

def strip_fileext (x: str) -> str:
	last_dot = x.rfind('.')
	last_sep = x.rfind(os.path.sep)

	# the last . is after the last /
	# or there's no /, but dot exists
	if last_dot >= 0 and last_sep < last_dot:
		# then, it's safe to strip
		return x[:last_dot]
	# there's no .
	return x


def proc_dir (dir: str):
	# including trailing '/' if desired
	s3_upload_prefix = os.getenv('MMFWD_RECSYNC_S3_UPLOAD_PREFIX')
	# minimum audio length: 5 seconds
	minsize = 5 * 1 * 2 * 16000 # seconds * nb_channels * bytes_per_sample * rate

	for path in glob.glob(dir + "/*.pcm"): # for each raw audio file,
		if not os.path.isfile(path):
			continue

		# do stat()
		stat = os.stat(path)

		# delete short recordings
		if stat.st_size < minsize:
			sys.stderr.write(
				"mmfwd.recsync: deleting short recording: " +
				path +
				os.linesep)
			os.remove(path)
			continue

		# ignore if less than an hour as a security measure against KYC attempts
		now = datetime.datetime.now(datetime.UTC)
		f_mtime = datetime.datetime.fromtimestamp(stat.st_mtime, datetime.UTC)
		if now - f_mtime < datetime.timedelta(hours = 1.0):
			continue

		# be defensive: align the original raw audio file to 2 bytes boundary
		# so that ffmpeg won't complain
		fsize = stat.st_size
		if fsize % 2 != 0:
			fsize = (fsize // 2) * 2
			os.truncate(path, fsize)

		# sep file ext, fabricate output file names
		basename = strip_fileext(os.path.basename(path))
		s3_basename = "%d-%02d/%s" % (f_mtime.year, f_mtime.month, basename)
		local_basepath = os.path.dirname(path) + '/' + basename
		s3_basepath = s3_upload_prefix + s3_basename
		local_outf_flac = local_basepath + ".flac"
		local_outf_ogg = local_basepath + ".ogg"
		s3_outf_flac = s3_basepath + ".flac"
		s3_outf_ogg = s3_basepath + ".ogg"

		# run ffmpeg for FLAC, lossless original not post processed
		subprocess.run([
			"ffmpeg", "-nostdin", "-loglevel", "error",
			"-f", "s16le", "-ac", "1", "-ar", "16000",
			"-i", path,
			"-y",
			local_outf_flac
		], check = True)
		# run ffmpeg for opus OGG w/ RMS normalisation
		subprocess.run([
			"ffmpeg", "-nostdin", "-loglevel", "error",
			"-f", "s16le", "-ac", "1", "-ar", "16000",
			"-i", path,
			"-filter:a", "loudnorm",
			"-c:a", "libopus",
			"-y",
			local_outf_ogg
		], check = True)

		# do upload
		subprocess.run([
			"aws", "s3", "cp", "--no-progress", local_outf_flac, s3_outf_flac
		], check = True)
		subprocess.run([
			"aws", "s3", "cp", "--no-progress", local_outf_ogg, s3_outf_ogg
		], check = True)

		# good! now delete files including the original from local
		os.remove(local_outf_flac)
		os.remove(local_outf_ogg)
		os.remove(path)

	# if the directory becomes empty
	dir_not_empty = True
	for _ in os.scandir(dir):
		dir_not_empty = False
		break
	if dir_not_empty: os.rmdir(dir)
