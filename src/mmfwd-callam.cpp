#include <iostream>
#include <fstream>
#include <vector>
#include <string>
#include <cstdint>
#include <unistd.h>
#include <fcntl.h>
#include <csignal>
#include <glob.h>
#include <sys/mman.h>
#include <poll.h>
#include <cassert>
#include <cerrno>
#include <cstring>
#include <termios.h>

// mmfwd call answering machine

// read a filename from stdin
// create the file and start recording
// until an empty string or a filename is read from stdin

// stty -F /dev/ttyUSB4 -drain raw -echo
// aplay -f S16_LE -r8000 -c1 /dev/ttyUSB4
// ffmpeg -re -i ... -ar 8000 -f s16le -ac 1 - > /dev/ttyUSB4

#define ARGV0 "mmfwd-callam"

struct {
	const char *dev;
	const char *hello_sample_pat; // glob patterns
	const char *outfile_prefix;
	struct {
		bool enabled[2];
	} playback;
	int vl;
} params;

struct {
	struct {
		std::string path;
		const void *m;
		size_t len;
	} hsample;
	std::string outfile[2];
	int fd_dev;
	int fd[2];
	int playback[2];
} g;


std::vector<uint8_t> geturnd (const size_t l) {
	std::vector<uint8_t> ret(l);
	std::ifstream f("/dev/urandom", std::ios_base::in | std::ios_base::binary);
	f.read((char*)ret.data(), l);
	return ret;
}

size_t getrndsize (void) {
	const auto r = geturnd(sizeof(size_t));
	return *(const size_t*)r.data();
}

void init_params (void) {
	params.hello_sample_pat = "hello/*.pcm";
	// params.playback.sample_rate = "8000";
	// params.playback.channels = "1";
	// params.playback.fmt = "S16_LE";
}

void init_g (void) {
	g.fd_dev = -1;
	g.fd[0] = g.fd[1] = -1;
	g.playback[0] = g.playback[1] = -1;
}

void parse_env (void) {
	const char *env;

	env = getenv("MMFWD_CALLAM_PLAYBACK");
	if (env != NULL) {
		int v = 0;

		sscanf(env, "%d", &v);
		params.playback.enabled[0] = v & 1 ? true : false;
		params.playback.enabled[1] = v & 2 ? true : false;
	}
}

pid_t do_playback_exec (const int p_stdin, const int p_stdout) {
	pid_t pid;
	int fr;

	pid = fork();
	if (pid < 0) {
		return -1;
	}
	else if (pid > 0) {
		return pid;
	}

	close(STDIN_FILENO);
	close(STDOUT_FILENO);
	dup2(p_stdin, STDIN_FILENO);
	dup2(p_stdout, STDOUT_FILENO);
	close(p_stdin);
	close(p_stdout);

	fr = system(
		"ffmpeg -loglevel error -f s16le -ar 16000 -ac 1 -i pipe: -filter:a loudnorm -ar 16000 -ac 1 -f s16le - | "
		"aplay -q -r 16000 -c 1 -f S16_LE -"
	);
	if (fr < 0) {
		perror(ARGV0 ": system()");
		abort();
	}
	exit(fr);

	assert("unreachable" && false);
}

bool open_playback (void) {
	int p[2];
	int fr;
	pid_t pid[2] = { -1, -1 };
	int fd_in[2] = { -1, -1 };
	int fd_out[2] = { -1, -1 };
	int blackhole;

	fr = pipe(p);
	if (fr < 0) {
		perror(ARGV0 ": pipe()");
		goto ERR;
	}
	close(p[0]);
	blackhole = p[1];

	for (size_t i = 0; i < 2; i += 1) {
		if (!params.playback.enabled[i]) {
			continue;
		}

		fr = pipe(p);
		if (fr < 0) {
			perror(ARGV0 ": pipe()");
			goto ERR;
		}
		fd_in[i] = p[0];
		fd_out[i] = p[1];

		pid[i] = do_playback_exec(p[0], blackhole);
		if (pid[i] < 0) {
			goto ERR;
		}

		g.playback[i] = p[1];
		close(fd_in[i]);
		fd_in[i] = -1;

		// Let audio skip when the buffer gets full.
		// Should be fine as long as the size of the buffer is aligned to 2 byte
		// boundary which is always the case.
		fr = fcntl(g.playback[i], F_GETFL);
		fr |= O_NONBLOCK;
		fr = fcntl(g.playback[i], F_SETFL);
		assert(fr == 0);
	}

	return true;
ERR: // house-keeping
	for (size_t i = 0; i < 2; i += 1) {
		close(fd_in[i]);
		close(fd_out[i]);
		kill(pid[i], SIGHUP);
	}
	return false;
}

bool open_dev (void) {
	struct termios tis;
	const int fd = open(params.dev, O_RDWR | O_NONBLOCK);

	if (fd < 0) {
		std::cerr << ARGV0 << ": ";
		perror(params.dev);
		return false;
	}

	// doesn't fucking work. just reset the damn modem
	// tcflush(fd, TCIOFLUSH);

	// make the serial raw
	tcgetattr(fd, &tis);
	cfmakeraw(&tis);
	tis.c_cflag &= ~CRTSCTS;
	tcsetattr(fd, TCSANOW, &tis);

	g.fd_dev = fd;

	return true;
}

bool open_hsample (void) {
	bool ret = false;
	glob_t gl;
	size_t n = getrndsize();
	int fr, fd = -1;
	off_t flen;
	const void *mapped;
	std::string errmsg, path;

	memset(&gl, 0, sizeof(gl));

	do {
		fr = glob(params.hello_sample_pat, GLOB_NOSORT, NULL, &gl);
		switch (fr) {
		case GLOB_ABORTED: errmsg = "read aborted"; break;
		case GLOB_NOMATCH: errmsg = "no match"; break;
		}
		if (fr != 0) {
			std::cerr << ARGV0 << ": " << params.hello_sample_pat << ": ";

			if (errmsg.empty()) {
				perror(NULL);
			}
			else {
				std::cerr << errmsg << std::endl;
			}
			break;
		}

		n = n % gl.gl_pathc;
		path = gl.gl_pathv[n];

		fd = open(path.c_str(), O_RDONLY);
		if (fd < 0) {
			std::cerr << ARGV0 << ": " << path << ": ";
			perror(NULL);
			break;
		}

		flen = lseek(fd, 0, SEEK_END);
		if (flen < 0) {
			std::cerr << ARGV0 << ": " << path << ": ";
			perror(NULL);
			break;
		}

		mapped = mmap(NULL, (size_t)flen, PROT_READ, MAP_PRIVATE, fd, 0);
		if (mapped == MAP_FAILED) {
			std::cerr << ARGV0 << ": " << path << ": ";
			perror(NULL);
			break;
		}

		g.hsample.m = mapped;
		g.hsample.len = (size_t)flen;
		g.hsample.path = std::move(path);
		ret = true;
		std::cerr << ARGV0 << ": hello sample: " << g.hsample.path << std::endl;
	} while (false);

	close(fd);
	globfree(&gl);
	return ret;
}

bool open_fds (void) {
	std::string path[2];

	path[0] = path[1] = params.outfile_prefix;
	path[0] += ".in.pcm";
	path[1] += ".out.pcm";

	for (size_t i = 0; i < 2; i += 1) {
		g.fd[i] = open(path[i].c_str(), O_WRONLY | O_CREAT, 0644);
		if (g.fd[i] < 0) {
			std::cerr << ARGV0 << ": " << path[i] << ": ";
			perror(NULL);

			return false;
		}
	}

	g.outfile[0] = std::move(path[0]);
	g.outfile[1] = std::move(path[1]);

	return true;
}

bool write_full (const int fd, const void *m, size_t len, const std::string &fn) {
	const uint8_t *ptr = (const uint8_t*)m;
	ssize_t iofr;

	while (len > 0) {
		iofr = write(fd, ptr, len);
		if (iofr < 0) {
			std::cerr << ARGV0 << ": " << fn << ": ";
			perror(NULL);
			return false;
		}
		assert(iofr != 0);

		ptr += iofr;
		len -= iofr;
	}

	return true;
}

void sink_playback_audio (int *fd, const void *buf, const size_t len) {
	ssize_t iofr;

	if (*fd < 0) {
		return;
	}

	iofr = write(*fd, buf, len);
	// Errors are not fatal:
	// - skip some samples when the child process struggle/stopped
	// - EPIPE in case of death of child process
	if (iofr < 0 && errno == EPIPE) {
		close(*fd);
		*fd = -1;
	}
}

bool loop_main (void) {
	uint8_t buf[4096];
	size_t sample_pos = 0;
	int fr;
	ssize_t iofr;
	size_t blen;
	struct pollfd pfd;

	memset(&pfd, 0, sizeof(pfd));

	pfd.fd = g.fd_dev;

	while (true) {
		if (sample_pos < g.hsample.len) {
			pfd.events = POLLIN | POLLOUT;
		}
		else {
			pfd.events = POLLIN;
		}

		fr = poll(&pfd, 1, -1);
		assert(fr != 0);
		if (fr < 0) {
			perror(ARGV0 ": poll()");
			abort();
		}

		if (pfd.events & POLLIN) {
			iofr = read(g.fd_dev, buf, sizeof(buf));
			if (iofr == 0) {
				std::cerr << ARGV0 << ": " << params.dev << ": read EOF reached" << std::endl;
				return true;
			}
			else if (iofr < 0) {
				if (errno == EAGAIN || errno == EWOULDBLOCK) {
					continue;
				}
				std::cerr << ARGV0 << ": " << params.dev << ": ";
				perror(NULL);
				return false;
			}
			blen = (size_t)iofr;

			if (params.vl > 1) {
				std::cerr << ARGV0 ": < " << iofr << std::endl;
			}

			if (!write_full(g.fd[0], buf, blen, g.outfile[0])) {
				return false;
			}
			sink_playback_audio(&g.playback[0], buf, blen);
		}

		if (pfd.events & POLLOUT) {
			const void *ptr = (const uint8_t*)g.hsample.m + sample_pos;

			iofr = write(g.fd_dev, ptr, g.hsample.len - sample_pos);
			if (iofr == 0) {
				std::cerr << ARGV0 << ": " << params.dev << ": write EOF reached" << std::endl;
				return true;
			}
			else if (iofr < 0) {
				if (errno == EAGAIN || errno == EWOULDBLOCK) {
					continue;
				}
				std::cerr << ARGV0 << ": " << params.dev << ": ";
				perror(NULL);
				return false;
			}
			blen = (size_t)iofr;

			if (params.vl > 1) {
				std::cerr << ARGV0 ": > " << iofr << std::endl;
			}

			if (!write_full(g.fd[1], ptr, blen, g.outfile[1])) {
				return false;
			}
			sink_playback_audio(&g.playback[1], buf, blen);

			sample_pos += blen;
			if (sample_pos >= g.hsample.len) {
				std::cerr << ARGV0 ": playback finished" << std::endl;
			}
		}
	}
}

void handle_termsig (int) {
	// doesn't fucking work. just reset the damn modem
	// tcflush(g.fd_dev, TCIOFLUSH);
	close(g.fd_dev);

	close(g.fd[0]);
	close(g.fd[1]);

	close(g.playback[0]);
	close(g.playback[1]);

	exit(0);
}

int main (const int argc, const char **argv) {
	init_params();
	init_g();

	parse_env();
	// TODO: getopt()

	if (argc <= 2) {
		std::cerr
			<< "Usage: " ARGV0 " <modem audio char path> <output prefix>"
			<< std::endl;
		return 2;
	}

	params.dev = argv[1];
	params.outfile_prefix = argv[2];

	signal(SIGPIPE, SIG_IGN);

	if (!open_playback() || !open_dev() || !open_hsample() || !open_fds()) {
		return 1;
	}

	return loop_main() ? 0 : 1;
}
