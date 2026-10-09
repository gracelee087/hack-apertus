.PHONY: run eval

# the project lives in track_2b/; `make run` works from either directory
run:
	$(MAKE) -C track_2b run

eval:
	$(MAKE) -C track_2b eval
