import React from 'react';
import { Trans } from '@lingui/macro';
import { Card, Flex, FormatBytes, Loading } from '@chia/core';
import { makeStyles } from '@material-ui/core/styles';
import { Paper, TableRow } from '@material-ui/core';
import Button from '@material-ui/core/Button';
import Table from '@material-ui/core/Table';
import TableBody from '@material-ui/core/TableBody';
import TableCell from '@material-ui/core/TableCell';
import TableContainer from '@material-ui/core/TableContainer';
import TableHead from '@material-ui/core/TableHead';
import DeleteForeverIcon from '@material-ui/icons/DeleteForever';
import TextField from '@material-ui/core/TextField';
import SettingsInputAntennaIcon from '@material-ui/icons/SettingsInputAntenna';
import { service_connection_types } from '../../util/service_names';

const useStyles = makeStyles((theme) => ({
  form: {
    margin: theme.spacing(1),
  },
  clickable: {
    cursor: 'pointer',
  },
  error: {
    color: 'red',
  },
  container: {
    paddingTop: theme.spacing(0),
    paddingBottom: theme.spacing(0),
    paddingRight: theme.spacing(0),
  },
  balancePaper: {
    marginTop: theme.spacing(2),
    padding: theme.spacing(2),
  },
  cardTitle: {
    paddingLeft: theme.spacing(1),
    paddingTop: theme.spacing(1),
    marginBottom: theme.spacing(1),
  },
  table: {
    minWidth: 650,
  },
  connect: {
    marginLeft: theme.spacing(1),
  },
}));

export default function Connections(props) {
  const classes = useStyles();

  const { connections } = props;
  const { connectionError } = props;

  const [host, setHost] = React.useState('');
  const handleChangeHost = (event) => {
    setHost(event.target.value);
  };

  const [port, setPort] = React.useState('');
  const handleChangePort = (event) => {
    setPort(event.target.value);
  };

  const deleteConnection = (node_id) => {
    return () => {
      props.closeConnection(node_id);
    };
  };
  const connectToPeer = () => {
    props.openConnection(host, port);
    setHost('');
    setPort('');
  };

  return (
    <Card
      title={<Trans id="Connections.title">Connections</Trans>}
    >
      {connections ? (
        <TableContainer component={Paper}>
          <Table
            className={classes.table}
            size="small"
            aria-label="a dense table"
          >
            <TableHead>
              <TableRow>
                <TableCell>
                  <Trans id="Connections.nodeId">Node ID</Trans>
                </TableCell>
                <TableCell align="right">
                  <Trans id="Connections.ipAddress">IP address</Trans>
                </TableCell>
                <TableCell align="right">
                  <Trans id="Connections.port">Port</Trans>
                </TableCell>
                <TableCell align="right">
                  <Trans id="Connections.upDown">MiB Up/Down</Trans>
                </TableCell>
                <TableCell align="right">
                  <Trans id="Connections.connectionType">
                    Connection type
                  </Trans>
                </TableCell>
                <TableCell align="right">
                  <Trans id="Connections.sbHeight">SB Height</Trans>
                </TableCell>
                <TableCell align="right">
                  <Trans id="Connections.delete">Delete</Trans>
                </TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {connections.map((item) => (
                <TableRow key={item.node_id}>
                  <TableCell component="th" scope="row">
                    {item.node_id.slice(0, 10)}...
                  </TableCell>
                  <TableCell align="right">{item.peer_host}</TableCell>
                  <TableCell align="right">
                    {item.peer_port}/{item.peer_server_port}
                  </TableCell>

                  <TableCell align="right">
                    <FormatBytes value={item.bytes_written} unit="MiB" removeUnit fixedDecimals />
                    /
                    <FormatBytes value={item.bytes_read} unit="MiB" removeUnit fixedDecimals />
                  </TableCell>
                  <TableCell align="right">
                    {service_connection_types[item.type]}
                  </TableCell>
                  <TableCell align="right">
                    {item.peak_sub_height}
                  </TableCell>
                  <TableCell
                    className={classes.clickable}
                    onClick={deleteConnection(item.node_id)}
                    align="right"
                  >
                    <DeleteForeverIcon />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      ) : (
        <Flex justifyContent="center">
          <Loading />
        </Flex>
      )}
      
      <h4 className={classes.connect}>
        <Trans id="Connections.connectToOtherPeersTitle">
          Connect to other peers
        </Trans>
      </h4>
      <form className={classes.form} noValidate autoComplete="off">
        <TextField
          label={
            <Trans id="Connections.ipAddressHost">IP address / host</Trans>
          }
          value={host}
          onChange={handleChangeHost}
        />
        <TextField
          label={<Trans id="Connections.port">Port</Trans>}
          value={port}
          onChange={handleChangePort}
        />
        <Button
          variant="contained"
          color="primary"
          onClick={connectToPeer}
          className={classes.button}
          startIcon={<SettingsInputAntennaIcon />}
        >
          <Trans id="Connections.connect">Connect</Trans>
        </Button>
      </form>
      {connectionError === '' ? (
        ''
      ) : (
        <p className={classes.error}>{connectionError}</p>
      )}
    </Card>
  );
}
