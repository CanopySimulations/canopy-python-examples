# Introduction

This repository contains a set of Jupyter Notebooks containing examples of how to use the [Canopy Python](https://github.com/CanopySimulations/canopy-python) library.

Each example tends to install a specific version of the Canopy library, for example `pip install -q 'canopy==7.3'`. This is so that the example continues to run even if we make breaking changes to the underlying Canopy Python library. In your own code you can either install a specific version or use the latest version with `pip install -q canopy`. If you do find some of the example code fails to run in the latest version of the library, let us know and we will update it.

If you find you need to update your Canopy library you can run `pip install --upgrade canopy`. If you run this from a Jupyter Notebook or other runtime, make sure you restart the runtime after upgrading to pick up the newly installed version.

Breaking changes to the Canopy Python library are documented in the [CHANGELOG.md](https://github.com/CanopySimulations/canopy-python/blob/master/CHANGELOG.md) file.

# Examples

## General Usage
 - [Loading and Saving Configs](https://github.com/CanopySimulations/canopy-python-examples/blob/master/loading_and_saving_configs.ipynb) takes you through the basics of accessing and creating configs on the platform.

 - [Running Studies](https://github.com/CanopySimulations/canopy-python-examples/blob/master/running_studies.ipynb) demonstrates loading configs and using them to run run studies on the platform, including how to ensure a study has completed before extracting the results.

- [Loading Vector Results](https://github.com/CanopySimulations/canopy-python-examples/blob/master/loading_vector_results.ipynb) shows how to load channel data from simulations for further analysis and visualization.

- [Loading Scalar Results](https://github.com/CanopySimulations/canopy-python-examples/blob/master/loading_scalar_results.ipynb) shows how to load scalar results from simulations for further analysis and visualization.

- [Loading All Vector Results](https://github.com/CanopySimulations/canopy-python-examples/blob/master/loading_all_vector_results.ipynb) shows how you would go about loading every single channel from a study by using the vector metadata to extract the list of channels.


## Specialised Tasks

- [Converting bump-stops from lookup to parametric](https://github.com/CanopySimulations/canopy-python-examples/blob/master/converting_bump_stops_from_lookup_to_parametric.ipynb) loads a car with a bump stop lookup, converts it to a parametric bump stop, and saves the result as a new car on the platform.

- [Finding the quickest 5 seconds to apply a power increase](https://github.com/CanopySimulations/canopy-python-examples/blob/master/finding_quickest_5_seconds_to_apply_power_increase.ipynb) uses the `dTLap_drEnginePowerFactor` channel to find the sLap ranges representing the optimal places to deploy 5 seconds of power increase.

- [Fitting aero data to a polynomial and RBF](https://github.com/CanopySimulations/canopy-python-examples/blob/master/fitting_aero_data_to_polynomial_and_rbf.ipynb) fits some data from a CSV file and saves the resulting polynomial and RBF aero definition to a car on the platform.

# Further Information

## Jupyter Notebooks

The examples use [Jupyter Notebooks](https://jupyter.org/) which can be viewed directly on GitHub. If you want to interact with the notebook, or create your own, you'll need to either install Juypyer Notebooks locally using something like the [Anaconda Distribution](https://www.anaconda.com/distribution/) or use one of the many online Jupyter Notebook hosts such as the excellent and free [Google Colab](https://colab.research.google.com/). 

Note that Google Colab uses a fairly old IPython runtime by default, so most of our examples start by upgrading the runtime to support the latest Python features, in particular async/await at the cell level. The process for running the examples after opening in Google Colab is therefore:
 - Run the "Upgrade Runtime" cell.
 - Click the button that appears to restart the runtime.
 - Run the rest of the notebook.


## Visualizations

Most of the examples use [Matplotlib](https://matplotlib.org/) which has the advantage of rendering in GitHub. However numerous other options are available such as [Seaborn](https://seaborn.pydata.org/) which sits on top of Matplotlib, [Bokeh](https://docs.bokeh.org/) which has great interactivity support and [Altair](https://altair-viz.github.io/).

## Data

The Canopy Python library uses [Pandas DataFrames](https://pandas.pydata.org/) for much of its data handling, which is part of the [SciPy](https://www.scipy.org/) ecosystem.

